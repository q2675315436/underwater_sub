import functools
import inspect

import mmcv
import numpy as np
# from numpy import random
import random
from mmdet.core import PolygonMasks
from mmdet.core.evaluation.bbox_overlaps import bbox_overlaps
from ..builder import PIPELINES
import cv2
import math

try:
    from imagecorruptions import corrupt
except ImportError:
    corrupt = None

try:
    import albumentations
    from albumentations import Compose
except ImportError:
    albumentations = None
    Compose = None


def imwrite_denormalized_debug_img(func):
    """Write denormalized debug image to file."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        """Write denormalized debug image to file."""
        results = func(*args, **kwargs)
        vis_img = mmcv.imdenormalize(
            results['img'],
            mean=np.array([123.675, 116.28, 103.53], dtype=np.float32),
            std=np.array([58.395, 57.12, 57.375], dtype=np.float32),
            to_bgr=True)
        random_img_id = np.random.randint(10)
        filename = f'debug_img_{random_img_id}.png'
        mmcv.imwrite(vis_img, filename)
        return results

    return wrapper


@PIPELINES.register_module()
class Resize(object):
    """Resize images & bbox & mask.

    This transform resizes the input image to some scale. Bboxes and masks are
    then resized with the same scale factor. If the input dict contains the key
    "scale", then the scale in the input dict is used, otherwise the specified
    scale in the init method is used. If the input dict contains the key
    "scale_factor" (if MultiScaleFlipAug does not give img_scale but
    scale_factor), the actual scale will be computed by image shape and
    scale_factor.

    `img_scale` can either be a tuple (single-scale) or a list of tuple
    (multi-scale). There are 3 multiscale modes:

    - ``ratio_range is not None``: randomly sample a ratio from the ratio \
      range and multiply it with the image scale.
    - ``ratio_range is None`` and ``multiscale_mode == "range"``: randomly \
      sample a scale from the multiscale range.
    - ``ratio_range is None`` and ``multiscale_mode == "value"``: randomly \
      sample a scale from multiple scales.

    Args:
        img_scale (tuple or list[tuple]): Images scales for resizing.
        multiscale_mode (str): Either "range" or "value".
        ratio_range (tuple[float]): (min_ratio, max_ratio)
        keep_ratio (bool): Whether to keep the aspect ratio when resizing the
            image.
        backend (str): Image resize backend, choices are 'cv2' and 'pillow'.
            These two backends generates slightly different results. Defaults
            to 'cv2'.
    """

    def __init__(self,
                 img_scale=None,
                 multiscale_mode='range',
                 ratio_range=None,
                 keep_ratio=True,
                 backend='cv2'):
        if img_scale is None:
            self.img_scale = None
        else:
            if isinstance(img_scale, list):
                self.img_scale = img_scale
            else:
                self.img_scale = [img_scale]
            assert mmcv.is_list_of(self.img_scale, tuple)

        if ratio_range is not None:
            # mode 1: given a scale and a range of image ratio
            assert len(self.img_scale) == 1
        else:
            # mode 2: given multiple scales or a range of scales
            assert multiscale_mode in ['value', 'range']

        self.backend = backend
        self.multiscale_mode = multiscale_mode
        self.ratio_range = ratio_range
        self.keep_ratio = keep_ratio

    @staticmethod
    def random_select(img_scales):
        """Randomly select an img_scale from given candidates.

        Args:
            img_scales (list[tuple]): Images scales for selection.

        Returns:
            (tuple, int): Returns a tuple ``(img_scale, scale_dix)``, \
                where ``img_scale`` is the selected image scale and \
                ``scale_idx`` is the selected index in the given candidates.
        """

        assert mmcv.is_list_of(img_scales, tuple)
        scale_idx = np.random.randint(len(img_scales))
        img_scale = img_scales[scale_idx]
        return img_scale, scale_idx

    @staticmethod
    def random_sample(img_scales):
        """Randomly sample an img_scale when ``multiscale_mode=='range'``.

        Args:
            img_scales (list[tuple]): Images scale range for sampling.
                There must be two tuples in img_scales, which specify the lower
                and uper bound of image scales.

        Returns:
            (tuple, None): Returns a tuple ``(img_scale, None)``, where \
                ``img_scale`` is sampled scale and None is just a placeholder \
                to be consistent with :func:`random_select`.
        """

        assert mmcv.is_list_of(img_scales, tuple) and len(img_scales) == 2
        img_scale_long = [max(s) for s in img_scales]
        img_scale_short = [min(s) for s in img_scales]
        long_edge = np.random.randint(
            min(img_scale_long),
            max(img_scale_long) + 1)
        short_edge = np.random.randint(
            min(img_scale_short),
            max(img_scale_short) + 1)
        img_scale = (long_edge, short_edge)
        return img_scale, None

    @staticmethod
    def random_sample_ratio(img_scale, ratio_range):
        """Randomly sample an img_scale when ``ratio_range`` is specified.

        A ratio will be randomly sampled from the range specified by
        ``ratio_range``. Then it would be multiplied with ``img_scale`` to
        generate sampled scale.

        Args:
            img_scale (tuple): Images scale base to multiply with ratio.
            ratio_range (tuple[float]): The minimum and maximum ratio to scale
                the ``img_scale``.

        Returns:
            (tuple, None): Returns a tuple ``(scale, None)``, where \
                ``scale`` is sampled ratio multiplied with ``img_scale`` and \
                None is just a placeholder to be consistent with \
                :func:`random_select`.
        """

        assert isinstance(img_scale, tuple) and len(img_scale) == 2
        min_ratio, max_ratio = ratio_range
        assert min_ratio <= max_ratio
        ratio = np.random.random_sample() * (max_ratio - min_ratio) + min_ratio
        scale = int(img_scale[0] * ratio), int(img_scale[1] * ratio)
        return scale, None

    def _random_scale(self, results):
        """Randomly sample an img_scale according to ``ratio_range`` and
        ``multiscale_mode``.

        If ``ratio_range`` is specified, a ratio will be sampled and be
        multiplied with ``img_scale``.
        If multiple scales are specified by ``img_scale``, a scale will be
        sampled according to ``multiscale_mode``.
        Otherwise, single scale will be used.

        Args:
            results (dict): Result dict from :obj:`dataset`.

        Returns:
            dict: Two new keys 'scale` and 'scale_idx` are added into \
                ``results``, which would be used by subsequent pipelines.
        """

        if self.ratio_range is not None:
            scale, scale_idx = self.random_sample_ratio(
                self.img_scale[0], self.ratio_range)
        elif len(self.img_scale) == 1:
            scale, scale_idx = self.img_scale[0], 0
        elif self.multiscale_mode == 'range':
            scale, scale_idx = self.random_sample(self.img_scale)
        elif self.multiscale_mode == 'value':
            scale, scale_idx = self.random_select(self.img_scale)
        else:
            raise NotImplementedError

        results['scale'] = scale
        results['scale_idx'] = scale_idx

    def _resize_img(self, results):
        """Resize images with ``results['scale']``."""
        for key in results.get('img_fields', ['img']):
            if self.keep_ratio:
                img, scale_factor = mmcv.imrescale(
                    results[key],
                    results['scale'],
                    return_scale=True,
                    backend=self.backend)
                # the w_scale and h_scale has minor difference
                # a real fix should be done in the mmcv.imrescale in the future
                new_h, new_w = img.shape[:2]
                h, w = results[key].shape[:2]
                w_scale = new_w / w
                h_scale = new_h / h
            else:
                img, w_scale, h_scale = mmcv.imresize(
                    results[key],
                    results['scale'],
                    return_scale=True,
                    backend=self.backend)
            results[key] = img

            scale_factor = np.array([w_scale, h_scale, w_scale, h_scale],
                                    dtype=np.float32)
            results['img_shape'] = img.shape
            # in case that there is no padding
            results['pad_shape'] = img.shape
            results['scale_factor'] = scale_factor
            results['keep_ratio'] = self.keep_ratio

    def _resize_bboxes(self, results):
        """Resize bounding boxes with ``results['scale_factor']``."""
        img_shape = results['img_shape']
        for key in results.get('bbox_fields', []):
            bboxes = results[key] * results['scale_factor']
            bboxes[:, 0::2] = np.clip(bboxes[:, 0::2], 0, img_shape[1])
            bboxes[:, 1::2] = np.clip(bboxes[:, 1::2], 0, img_shape[0])
            results[key] = bboxes

    def _resize_masks(self, results):
        """Resize masks with ``results['scale']``"""
        for key in results.get('mask_fields', []):
            if results[key] is None:
                continue
            if self.keep_ratio:
                results[key] = results[key].rescale(results['scale'])
            else:
                results[key] = results[key].resize(results['img_shape'][:2])

    def _resize_seg(self, results):
        """Resize semantic segmentation map with ``results['scale']``."""
        for key in results.get('seg_fields', []):
            if self.keep_ratio:
                gt_seg = mmcv.imrescale(
                    results[key],
                    results['scale'],
                    interpolation='nearest',
                    backend=self.backend)
            else:
                gt_seg = mmcv.imresize(
                    results[key],
                    results['scale'],
                    interpolation='nearest',
                    backend=self.backend)
            results['gt_semantic_seg'] = gt_seg

    def __call__(self, results):
        """Call function to resize images, bounding boxes, masks, semantic
        segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Resized results, 'img_shape', 'pad_shape', 'scale_factor', \
                'keep_ratio' keys are added into result dict.
        """

        if 'scale' not in results:
            if 'scale_factor' in results:
                img_shape = results['img'].shape[:2]
                scale_factor = results['scale_factor']
                assert isinstance(scale_factor, float)
                results['scale'] = tuple(
                    [int(x * scale_factor) for x in img_shape][::-1])
            else:
                self._random_scale(results)
        else:
            assert 'scale_factor' not in results, (
                'scale and scale_factor cannot be both set.')

        self._resize_img(results)
        self._resize_bboxes(results)
        self._resize_masks(results)
        self._resize_seg(results)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(img_scale={self.img_scale}, '
        repr_str += f'multiscale_mode={self.multiscale_mode}, '
        repr_str += f'ratio_range={self.ratio_range}, '
        repr_str += f'keep_ratio={self.keep_ratio})'
        return repr_str


@PIPELINES.register_module()
class RandomFlip(object):
    """Flip the image & bbox & mask.

    If the input dict contains the key "flip", then the flag will be used,
    otherwise it will be randomly decided by a ratio specified in the init
    method.

    When random flip is enabled, ``flip_ratio``/``direction`` can either be a
    float/string or tuple of float/string. There are 3 flip modes:

    - ``flip_ratio`` is float, ``direction`` is string: the image will be
        ``direction``ly flipped with probability of ``flip_ratio`` .
        E.g., ``flip_ratio=0.5``, ``direction='horizontal'``,
        then image will be horizontally flipped with probability of 0.5.
    - ``flip_ratio`` is float, ``direction`` is list of string: the image wil
        be ``direction[i]``ly flipped with probability of
        ``flip_ratio/len(direction)``.
        E.g., ``flip_ratio=0.5``, ``direction=['horizontal', 'vertical']``,
        then image will be horizontally flipped with probability of 0.25,
        vertically with probability of 0.25.
    - ``flip_ratio`` is list of float, ``direction`` is list of string:
        given ``len(flip_ratio) == len(direction)``, the image wil
        be ``direction[i]``ly flipped with probability of ``flip_ratio[i]``.
        E.g., ``flip_ratio=[0.3, 0.5]``, ``direction=['horizontal',
        'vertical']``, then image will be horizontally flipped with probability
         of 0.3, vertically with probability of 0.5

    Args:
        flip_ratio (float | list[float], optional): The flipping probability.
            Default: None.
        direction(str | list[str], optional): The flipping direction. Options
            are 'horizontal', 'vertical', 'diagonal'. Default: 'horizontal'.
            If input is a list, the length must equal ``flip_ratio``. Each
            element in ``flip_ratio`` indicates the flip probability of
            corresponding direction.
    """

    def __init__(self, flip_ratio=None, direction='horizontal'):
        if isinstance(flip_ratio, list):
            assert mmcv.is_list_of(flip_ratio, float)
            assert 0 <= sum(flip_ratio) <= 1
        elif isinstance(flip_ratio, float):
            assert 0 <= flip_ratio <= 1
        elif flip_ratio is None:
            pass
        else:
            raise ValueError('flip_ratios must be None, float, '
                             'or list of float')
        self.flip_ratio = flip_ratio

        valid_directions = ['horizontal', 'vertical', 'diagonal']
        if isinstance(direction, str):
            assert direction in valid_directions
        elif isinstance(direction, list):
            assert mmcv.is_list_of(direction, str)
            assert set(direction).issubset(set(valid_directions))
        else:
            raise ValueError('direction must be either str or list of str')
        self.direction = direction

        if isinstance(flip_ratio, list):
            assert len(self.flip_ratio) == len(self.direction)

    def bbox_flip(self, bboxes, img_shape, direction):
        """Flip bboxes horizontally.

        Args:
            bboxes (numpy.ndarray): Bounding boxes, shape (..., 4*k)
            img_shape (tuple[int]): Image shape (height, width)
            direction (str): Flip direction. Options are 'horizontal',
                'vertical'.

        Returns:
            numpy.ndarray: Flipped bounding boxes.
        """

        assert bboxes.shape[-1] % 4 == 0
        flipped = bboxes.copy()
        if direction == 'horizontal':
            w = img_shape[1]
            flipped[..., 0::4] = w - bboxes[..., 2::4]
            flipped[..., 2::4] = w - bboxes[..., 0::4]
        elif direction == 'vertical':
            h = img_shape[0]
            flipped[..., 1::4] = h - bboxes[..., 3::4]
            flipped[..., 3::4] = h - bboxes[..., 1::4]
        elif direction == 'diagonal':
            w = img_shape[1]
            h = img_shape[0]
            flipped[..., 0::4] = w - bboxes[..., 2::4]
            flipped[..., 1::4] = h - bboxes[..., 3::4]
            flipped[..., 2::4] = w - bboxes[..., 0::4]
            flipped[..., 3::4] = h - bboxes[..., 1::4]
        else:
            raise ValueError(f"Invalid flipping direction '{direction}'")
        return flipped

    def __call__(self, results):
        """Call function to flip bounding boxes, masks, semantic segmentation
        maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Flipped results, 'flip', 'flip_direction' keys are added \
                into result dict.
        """

        if 'flip' not in results:
            if isinstance(self.direction, list):
                # None means non-flip
                direction_list = self.direction + [None]
            else:
                # None means non-flip
                direction_list = [self.direction, None]

            if isinstance(self.flip_ratio, list):
                non_flip_ratio = 1 - sum(self.flip_ratio)
                flip_ratio_list = self.flip_ratio + [non_flip_ratio]
            else:
                non_flip_ratio = 1 - self.flip_ratio
                # exclude non-flip
                single_ratio = self.flip_ratio / (len(direction_list) - 1)
                flip_ratio_list = [single_ratio] * (len(direction_list) -
                                                    1) + [non_flip_ratio]

            cur_dir = np.random.choice(direction_list, p=flip_ratio_list)

            results['flip'] = cur_dir is not None
        if 'flip_direction' not in results:
            results['flip_direction'] = cur_dir
        if results['flip']:
            # flip image
            for key in results.get('img_fields', ['img']):
                results[key] = mmcv.imflip(
                    results[key], direction=results['flip_direction'])
            # flip bboxes
            for key in results.get('bbox_fields', []):
                results[key] = self.bbox_flip(results[key],
                                              results['img_shape'],
                                              results['flip_direction'])
            # flip masks
            for key in results.get('mask_fields', []):
                results[key] = results[key].flip(results['flip_direction'])

            # flip segs
            for key in results.get('seg_fields', []):
                results[key] = mmcv.imflip(
                    results[key], direction=results['flip_direction'])
        return results

    def __repr__(self):
        return self.__class__.__name__ + f'(flip_ratio={self.flip_ratio})'


@PIPELINES.register_module()
class Pad(object):
    """Pad the image & mask.

    There are two padding modes: (1) pad to a fixed size and (2) pad to the
    minimum size that is divisible by some number.
    Added keys are "pad_shape", "pad_fixed_size", "pad_size_divisor",

    Args:
        size (tuple, optional): Fixed padding size.
        size_divisor (int, optional): The divisor of padded size.
        pad_val (float, optional): Padding value, 0 by default.
    """

    def __init__(self, size=None, size_divisor=None, pad_val=0):
        self.size = size
        self.size_divisor = size_divisor
        self.pad_val = pad_val
        # only one of size and size_divisor should be valid
        assert size is not None or size_divisor is not None
        assert size is None or size_divisor is None

    def _pad_img(self, results):
        """Pad images according to ``self.size``."""
        for key in results.get('img_fields', ['img']):
            if self.size is not None:
                padded_img = mmcv.impad(
                    results[key], shape=self.size, pad_val=self.pad_val)
            elif self.size_divisor is not None:
                padded_img = mmcv.impad_to_multiple(
                    results[key], self.size_divisor, pad_val=self.pad_val)
            results[key] = padded_img
        results['pad_shape'] = padded_img.shape
        results['pad_fixed_size'] = self.size
        results['pad_size_divisor'] = self.size_divisor

    def _pad_masks(self, results):
        """Pad masks according to ``results['pad_shape']``."""
        pad_shape = results['pad_shape'][:2]
        for key in results.get('mask_fields', []):
            results[key] = results[key].pad(pad_shape, pad_val=self.pad_val)

    def _pad_seg(self, results):
        """Pad semantic segmentation map according to
        ``results['pad_shape']``."""
        for key in results.get('seg_fields', []):
            results[key] = mmcv.impad(
                results[key], shape=results['pad_shape'][:2])

    def __call__(self, results):
        """Call function to pad images, masks, semantic segmentation maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Updated result dict.
        """
        self._pad_img(results)
        self._pad_masks(results)
        self._pad_seg(results)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(size={self.size}, '
        repr_str += f'size_divisor={self.size_divisor}, '
        repr_str += f'pad_val={self.pad_val})'
        return repr_str


@PIPELINES.register_module()
class Normalize(object):
    """Normalize the image.

    Added key is "img_norm_cfg".

    Args:
        mean (sequence): Mean values of 3 channels.
        std (sequence): Std values of 3 channels.
        to_rgb (bool): Whether to convert the image from BGR to RGB,
            default is true.
    """

    def __init__(self, mean, std, to_rgb=True):
        self.mean = np.array(mean, dtype=np.float32)
        self.std = np.array(std, dtype=np.float32)
        self.to_rgb = to_rgb

    def __call__(self, results):
        """Call function to normalize images.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Normalized results, 'img_norm_cfg' key is added into
                result dict.
        """
        for key in results.get('img_fields', ['img']):
            results[key] = mmcv.imnormalize(results[key], self.mean, self.std,
                                            self.to_rgb)
        results['img_norm_cfg'] = dict(
            mean=self.mean, std=self.std, to_rgb=self.to_rgb)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(mean={self.mean}, std={self.std}, to_rgb={self.to_rgb})'
        return repr_str


@PIPELINES.register_module()
class ResizeCAM12(object):
    """Resize images & bbox & mask.

    This transform resizes the input image to some scale. Bboxes and masks are
    then resized with the same scale factor. If the input dict contains the key
    "scale", then the scale in the input dict is used, otherwise the specified
    scale in the init method is used. If the input dict contains the key
    "scale_factor" (if MultiScaleFlipAug does not give img_scale but
    scale_factor), the actual scale will be computed by image shape and
    scale_factor.

    `img_scale` can either be a tuple (single-scale) or a list of tuple
    (multi-scale). There are 3 multiscale modes:

    - ``ratio_range is not None``: randomly sample a ratio from the ratio \
      range and multiply it with the image scale.
    - ``ratio_range is None`` and ``multiscale_mode == "range"``: randomly \
      sample a scale from the multiscale range.
    - ``ratio_range is None`` and ``multiscale_mode == "value"``: randomly \
      sample a scale from multiple scales.

    Args:
        img_scale (tuple or list[tuple]): Images scales for resizing.
        multiscale_mode (str): Either "range" or "value".
        ratio_range (tuple[float]): (min_ratio, max_ratio)
        keep_ratio (bool): Whether to keep the aspect ratio when resizing the
            image.
        backend (str): Image resize backend, choices are 'cv2' and 'pillow'.
            These two backends generates slightly different results. Defaults
            to 'cv2'.
    """

    def __init__(self,
                 img_scale=None,
                 multiscale_mode='range',
                 ratio_range=None,
                 keep_ratio=True,
                 backend='cv2'):
        if img_scale is None:
            self.img_scale = None
        else:
            if isinstance(img_scale, list):
                self.img_scale = img_scale
            else:
                self.img_scale = [img_scale]
            assert mmcv.is_list_of(self.img_scale, tuple)

        if ratio_range is not None:
            # mode 1: given a scale and a range of image ratio
            assert len(self.img_scale) == 1
        else:
            # mode 2: given multiple scales or a range of scales
            assert multiscale_mode in ['value', 'range']

        self.backend = backend
        self.multiscale_mode = multiscale_mode
        self.ratio_range = ratio_range
        self.keep_ratio = keep_ratio

    @staticmethod
    def random_select(img_scales):
        """Randomly select an img_scale from given candidates.

        Args:
            img_scales (list[tuple]): Images scales for selection.

        Returns:
            (tuple, int): Returns a tuple ``(img_scale, scale_dix)``, \
                where ``img_scale`` is the selected image scale and \
                ``scale_idx`` is the selected index in the given candidates.
        """

        assert mmcv.is_list_of(img_scales, tuple)
        scale_idx = np.random.randint(len(img_scales))
        img_scale = img_scales[scale_idx]
        return img_scale, scale_idx

    @staticmethod
    def random_sample(img_scales):
        """Randomly sample an img_scale when ``multiscale_mode=='range'``.

        Args:
            img_scales (list[tuple]): Images scale range for sampling.
                There must be two tuples in img_scales, which specify the lower
                and uper bound of image scales.

        Returns:
            (tuple, None): Returns a tuple ``(img_scale, None)``, where \
                ``img_scale`` is sampled scale and None is just a placeholder \
                to be consistent with :func:`random_select`.
        """

        assert mmcv.is_list_of(img_scales, tuple) and len(img_scales) == 2
        img_scale_long = [max(s) for s in img_scales]
        img_scale_short = [min(s) for s in img_scales]
        long_edge = np.random.randint(
            min(img_scale_long),
            max(img_scale_long) + 1)
        short_edge = np.random.randint(
            min(img_scale_short),
            max(img_scale_short) + 1)
        img_scale = (long_edge, short_edge)
        return img_scale, None

    @staticmethod
    def random_sample_ratio(img_scale, ratio_range):
        """Randomly sample an img_scale when ``ratio_range`` is specified.

        A ratio will be randomly sampled from the range specified by
        ``ratio_range``. Then it would be multiplied with ``img_scale`` to
        generate sampled scale.

        Args:
            img_scale (tuple): Images scale base to multiply with ratio.
            ratio_range (tuple[float]): The minimum and maximum ratio to scale
                the ``img_scale``.

        Returns:
            (tuple, None): Returns a tuple ``(scale, None)``, where \
                ``scale`` is sampled ratio multiplied with ``img_scale`` and \
                None is just a placeholder to be consistent with \
                :func:`random_select`.
        """

        assert isinstance(img_scale, tuple) and len(img_scale) == 2
        min_ratio, max_ratio = ratio_range
        assert min_ratio <= max_ratio
        ratio = np.random.random_sample() * (max_ratio - min_ratio) + min_ratio
        scale = int(img_scale[0] * ratio), int(img_scale[1] * ratio)
        return scale, None

    def _random_scale(self, results):
        """Randomly sample an img_scale according to ``ratio_range`` and
        ``multiscale_mode``.

        If ``ratio_range`` is specified, a ratio will be sampled and be
        multiplied with ``img_scale``.
        If multiple scales are specified by ``img_scale``, a scale will be
        sampled according to ``multiscale_mode``.
        Otherwise, single scale will be used.

        Args:
            results (dict): Result dict from :obj:`dataset`.

        Returns:
            dict: Two new keys 'scale` and 'scale_idx` are added into \
                ``results``, which would be used by subsequent pipelines.
        """

        if self.ratio_range is not None:
            scale, scale_idx = self.random_sample_ratio(
                self.img_scale[0], self.ratio_range)
        elif len(self.img_scale) == 1:
            scale, scale_idx = self.img_scale[0], 0
        elif self.multiscale_mode == 'range':
            scale, scale_idx = self.random_sample(self.img_scale)
        elif self.multiscale_mode == 'value':
            scale, scale_idx = self.random_select(self.img_scale)
        else:
            raise NotImplementedError

        results['scale'] = scale
        results['scale_idx'] = scale_idx

    def _resize_img(self, results):
        """Resize images with ``results['scale']``."""
        for key in results.get('img_fields', ['img']):
            if self.keep_ratio:
                img, scale_factor = mmcv.imrescale(
                    results[key],
                    results['scale'],
                    return_scale=True,
                    backend=self.backend)
                # the w_scale and h_scale has minor difference
                # a real fix should be done in the mmcv.imrescale in the future
                new_h, new_w = img.shape[:2]
                h, w = results[key].shape[:2]
                w_scale = new_w / w
                h_scale = new_h / h
            else:
                img, w_scale, h_scale = mmcv.imresize(
                    results[key],
                    results['scale'],
                    return_scale=True,
                    backend=self.backend)
            results[key] = img

            scale_factor = np.array([w_scale, h_scale, w_scale, h_scale],
                                    dtype=np.float32)
            results['img_shape'] = img.shape
            # in case that there is no padding
            results['pad_shape'] = img.shape
            results['scale_factor'] = scale_factor
            results['keep_ratio'] = self.keep_ratio

    def _resize_bboxes(self, results):
        """Resize bounding boxes with ``results['scale_factor']``."""
        img_shape = results['img_shape']
        for key in results.get('bbox_fields', []):
            bboxes = results[key] * results['scale_factor']
            bboxes[:, 0::2] = np.clip(bboxes[:, 0::2], 0, img_shape[1])
            bboxes[:, 1::2] = np.clip(bboxes[:, 1::2], 0, img_shape[0])
            results[key] = bboxes

    def _resize_masks(self, results):
        """Resize masks with ``results['scale']``"""
        for key in results.get('mask_fields', []):
            if results[key] is None:
                continue
            if self.keep_ratio:
                results[key] = results[key].rescale(results['scale'])
            else:
                results[key] = results[key].resize(results['img_shape'][:2])

    def _resize_seg(self, results):
        """Resize semantic segmentation map with ``results['scale']``."""
        for key in results.get('seg_fields', []):
            if self.keep_ratio:
                gt_seg = mmcv.imrescale(
                    results[key],
                    results['scale'],
                    interpolation='nearest',
                    backend=self.backend)
            else:
                gt_seg = mmcv.imresize(
                    results[key],
                    results['scale'],
                    interpolation='nearest',
                    backend=self.backend)
            results['gt_semantic_seg'] = gt_seg

    def __call__(self, results):
        """Call function to resize images, bounding boxes, masks, semantic
        segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Resized results, 'img_shape', 'pad_shape', 'scale_factor', \
                'keep_ratio' keys are added into result dict.
        """
        if 'CAM3' in results['filename']:
            return results
        assert 'CAM1' in results['filename'] or 'CAM2' in results['filename']
        if 'scale' not in results:
            if 'scale_factor' in results:
                img_shape = results['img'].shape[:2]
                scale_factor = results['scale_factor']
                assert isinstance(scale_factor, float)
                results['scale'] = tuple(
                    [int(x * scale_factor) for x in img_shape][::-1])
            else:
                self._random_scale(results)
        else:
            assert 'scale_factor' not in results, (
                'scale and scale_factor cannot be both set.')

        self._resize_img(results)
        self._resize_bboxes(results)
        self._resize_masks(results)
        self._resize_seg(results)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(img_scale={self.img_scale}, '
        repr_str += f'multiscale_mode={self.multiscale_mode}, '
        repr_str += f'ratio_range={self.ratio_range}, '
        repr_str += f'keep_ratio={self.keep_ratio})'
        return repr_str


@PIPELINES.register_module()
class ResizeCAM3(object):
    """Resize images & bbox & mask.

    This transform resizes the input image to some scale. Bboxes and masks are
    then resized with the same scale factor. If the input dict contains the key
    "scale", then the scale in the input dict is used, otherwise the specified
    scale in the init method is used. If the input dict contains the key
    "scale_factor" (if MultiScaleFlipAug does not give img_scale but
    scale_factor), the actual scale will be computed by image shape and
    scale_factor.

    `img_scale` can either be a tuple (single-scale) or a list of tuple
    (multi-scale). There are 3 multiscale modes:

    - ``ratio_range is not None``: randomly sample a ratio from the ratio \
      range and multiply it with the image scale.
    - ``ratio_range is None`` and ``multiscale_mode == "range"``: randomly \
      sample a scale from the multiscale range.
    - ``ratio_range is None`` and ``multiscale_mode == "value"``: randomly \
      sample a scale from multiple scales.

    Args:
        img_scale (tuple or list[tuple]): Images scales for resizing.
        multiscale_mode (str): Either "range" or "value".
        ratio_range (tuple[float]): (min_ratio, max_ratio)
        keep_ratio (bool): Whether to keep the aspect ratio when resizing the
            image.
        backend (str): Image resize backend, choices are 'cv2' and 'pillow'.
            These two backends generates slightly different results. Defaults
            to 'cv2'.
    """

    def __init__(self,
                 img_scale=None,
                 multiscale_mode='range',
                 ratio_range=None,
                 keep_ratio=True,
                 backend='cv2'):
        if img_scale is None:
            self.img_scale = None
        else:
            if isinstance(img_scale, list):
                self.img_scale = img_scale
            else:
                self.img_scale = [img_scale]
            assert mmcv.is_list_of(self.img_scale, tuple)

        if ratio_range is not None:
            # mode 1: given a scale and a range of image ratio
            assert len(self.img_scale) == 1
        else:
            # mode 2: given multiple scales or a range of scales
            assert multiscale_mode in ['value', 'range']

        self.backend = backend
        self.multiscale_mode = multiscale_mode
        self.ratio_range = ratio_range
        self.keep_ratio = keep_ratio

    @staticmethod
    def random_select(img_scales):
        """Randomly select an img_scale from given candidates.

        Args:
            img_scales (list[tuple]): Images scales for selection.

        Returns:
            (tuple, int): Returns a tuple ``(img_scale, scale_dix)``, \
                where ``img_scale`` is the selected image scale and \
                ``scale_idx`` is the selected index in the given candidates.
        """

        assert mmcv.is_list_of(img_scales, tuple)
        scale_idx = np.random.randint(len(img_scales))
        img_scale = img_scales[scale_idx]
        return img_scale, scale_idx

    @staticmethod
    def random_sample(img_scales):
        """Randomly sample an img_scale when ``multiscale_mode=='range'``.

        Args:
            img_scales (list[tuple]): Images scale range for sampling.
                There must be two tuples in img_scales, which specify the lower
                and uper bound of image scales.

        Returns:
            (tuple, None): Returns a tuple ``(img_scale, None)``, where \
                ``img_scale`` is sampled scale and None is just a placeholder \
                to be consistent with :func:`random_select`.
        """

        assert mmcv.is_list_of(img_scales, tuple) and len(img_scales) == 2
        img_scale_long = [max(s) for s in img_scales]
        img_scale_short = [min(s) for s in img_scales]
        long_edge = np.random.randint(
            min(img_scale_long),
            max(img_scale_long) + 1)
        short_edge = np.random.randint(
            min(img_scale_short),
            max(img_scale_short) + 1)
        img_scale = (long_edge, short_edge)
        return img_scale, None

    @staticmethod
    def random_sample_ratio(img_scale, ratio_range):
        """Randomly sample an img_scale when ``ratio_range`` is specified.

        A ratio will be randomly sampled from the range specified by
        ``ratio_range``. Then it would be multiplied with ``img_scale`` to
        generate sampled scale.

        Args:
            img_scale (tuple): Images scale base to multiply with ratio.
            ratio_range (tuple[float]): The minimum and maximum ratio to scale
                the ``img_scale``.

        Returns:
            (tuple, None): Returns a tuple ``(scale, None)``, where \
                ``scale`` is sampled ratio multiplied with ``img_scale`` and \
                None is just a placeholder to be consistent with \
                :func:`random_select`.
        """

        assert isinstance(img_scale, tuple) and len(img_scale) == 2
        min_ratio, max_ratio = ratio_range
        assert min_ratio <= max_ratio
        ratio = np.random.random_sample() * (max_ratio - min_ratio) + min_ratio
        scale = int(img_scale[0] * ratio), int(img_scale[1] * ratio)
        return scale, None

    def _random_scale(self, results):
        """Randomly sample an img_scale according to ``ratio_range`` and
        ``multiscale_mode``.

        If ``ratio_range`` is specified, a ratio will be sampled and be
        multiplied with ``img_scale``.
        If multiple scales are specified by ``img_scale``, a scale will be
        sampled according to ``multiscale_mode``.
        Otherwise, single scale will be used.

        Args:
            results (dict): Result dict from :obj:`dataset`.

        Returns:
            dict: Two new keys 'scale` and 'scale_idx` are added into \
                ``results``, which would be used by subsequent pipelines.
        """

        if self.ratio_range is not None:
            scale, scale_idx = self.random_sample_ratio(
                self.img_scale[0], self.ratio_range)
        elif len(self.img_scale) == 1:
            scale, scale_idx = self.img_scale[0], 0
        elif self.multiscale_mode == 'range':
            scale, scale_idx = self.random_sample(self.img_scale)
        elif self.multiscale_mode == 'value':
            scale, scale_idx = self.random_select(self.img_scale)
        else:
            raise NotImplementedError

        results['scale'] = scale
        results['scale_idx'] = scale_idx

    def _resize_img(self, results):
        """Resize images with ``results['scale']``."""
        for key in results.get('img_fields', ['img']):
            if self.keep_ratio:
                img, scale_factor = mmcv.imrescale(
                    results[key],
                    results['scale'],
                    return_scale=True,
                    backend=self.backend)
                # the w_scale and h_scale has minor difference
                # a real fix should be done in the mmcv.imrescale in the future
                new_h, new_w = img.shape[:2]
                h, w = results[key].shape[:2]
                w_scale = new_w / w
                h_scale = new_h / h
            else:
                img, w_scale, h_scale = mmcv.imresize(
                    results[key],
                    results['scale'],
                    return_scale=True,
                    backend=self.backend)
            results[key] = img

            scale_factor = np.array([w_scale, h_scale, w_scale, h_scale],
                                    dtype=np.float32)
            results['img_shape'] = img.shape
            # in case that there is no padding
            results['pad_shape'] = img.shape
            results['scale_factor'] = scale_factor
            results['keep_ratio'] = self.keep_ratio

    def _resize_bboxes(self, results):
        """Resize bounding boxes with ``results['scale_factor']``."""
        img_shape = results['img_shape']
        for key in results.get('bbox_fields', []):
            bboxes = results[key] * results['scale_factor']
            bboxes[:, 0::2] = np.clip(bboxes[:, 0::2], 0, img_shape[1])
            bboxes[:, 1::2] = np.clip(bboxes[:, 1::2], 0, img_shape[0])
            results[key] = bboxes

    def _resize_masks(self, results):
        """Resize masks with ``results['scale']``"""
        for key in results.get('mask_fields', []):
            if results[key] is None:
                continue
            if self.keep_ratio:
                results[key] = results[key].rescale(results['scale'])
            else:
                results[key] = results[key].resize(results['img_shape'][:2])

    def _resize_seg(self, results):
        """Resize semantic segmentation map with ``results['scale']``."""
        for key in results.get('seg_fields', []):
            if self.keep_ratio:
                gt_seg = mmcv.imrescale(
                    results[key],
                    results['scale'],
                    interpolation='nearest',
                    backend=self.backend)
            else:
                gt_seg = mmcv.imresize(
                    results[key],
                    results['scale'],
                    interpolation='nearest',
                    backend=self.backend)
            results['gt_semantic_seg'] = gt_seg

    def __call__(self, results):
        """Call function to resize images, bounding boxes, masks, semantic
        segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Resized results, 'img_shape', 'pad_shape', 'scale_factor', \
                'keep_ratio' keys are added into result dict.
        """
        if 'CAM3' not in results['filename']:
            return results
        if 'scale' not in results:
            if 'scale_factor' in results:
                img_shape = results['img'].shape[:2]
                scale_factor = results['scale_factor']
                assert isinstance(scale_factor, float)
                results['scale'] = tuple(
                    [int(x * scale_factor) for x in img_shape][::-1])
            else:
                self._random_scale(results)
        else:
            assert 'scale_factor' not in results, (
                'scale and scale_factor cannot be both set.')

        self._resize_img(results)
        self._resize_bboxes(results)
        self._resize_masks(results)
        self._resize_seg(results)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(img_scale={self.img_scale}, '
        repr_str += f'multiscale_mode={self.multiscale_mode}, '
        repr_str += f'ratio_range={self.ratio_range}, '
        repr_str += f'keep_ratio={self.keep_ratio})'
        return repr_str


@PIPELINES.register_module()
class RandomCrop(object):
    """Random crop the image & bboxes & masks.

    Args:
        crop_size (tuple): Expected size after cropping, (h, w).
        allow_negative_crop (bool): Whether to allow a crop that does not
            contain any bbox area. Default to False.

    Note:
        - If the image is smaller than the crop size, return the original image
        - The keys for bboxes, labels and masks must be aligned. That is,
          `gt_bboxes` corresponds to `gt_labels` and `gt_masks`, and
          `gt_bboxes_ignore` corresponds to `gt_labels_ignore` and
          `gt_masks_ignore`.
        - If the crop does not contain any gt-bbox region and
          `allow_negative_crop` is set to False, skip this image.
    """

    def __init__(self, crop_size, allow_negative_crop=False):
        assert crop_size[0] > 0 and crop_size[1] > 0
        self.crop_size = crop_size
        self.allow_negative_crop = allow_negative_crop
        # The key correspondence from bboxes to labels and masks.
        self.bbox2label = {
            'gt_bboxes': 'gt_labels',
            'gt_bboxes_ignore': 'gt_labels_ignore'
        }
        self.bbox2mask = {
            'gt_bboxes': 'gt_masks',
            'gt_bboxes_ignore': 'gt_masks_ignore'
        }

    def __call__(self, results):
        """Call function to randomly crop images, bounding boxes, masks,
        semantic segmentation maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Randomly cropped results, 'img_shape' key in result dict is
                updated according to crop size.
        """

        for key in results.get('img_fields', ['img']):
            img = results[key]
            margin_h = max(img.shape[0] - self.crop_size[0], 0)
            margin_w = max(img.shape[1] - self.crop_size[1], 0)
            offset_h = np.random.randint(0, margin_h + 1)
            offset_w = np.random.randint(0, margin_w + 1)
            crop_y1, crop_y2 = offset_h, offset_h + self.crop_size[0]
            crop_x1, crop_x2 = offset_w, offset_w + self.crop_size[1]

            # crop the image
            img = img[crop_y1:crop_y2, crop_x1:crop_x2, ...]
            img_shape = img.shape
            results[key] = img
        results['img_shape'] = img_shape

        # crop bboxes accordingly and clip to the image boundary
        for key in results.get('bbox_fields', []):
            # e.g. gt_bboxes and gt_bboxes_ignore
            bbox_offset = np.array([offset_w, offset_h, offset_w, offset_h],
                                   dtype=np.float32)
            bboxes = results[key] - bbox_offset
            bboxes[:, 0::2] = np.clip(bboxes[:, 0::2], 0, img_shape[1])
            bboxes[:, 1::2] = np.clip(bboxes[:, 1::2], 0, img_shape[0])
            valid_inds = (bboxes[:, 2] > bboxes[:, 0]) & (
                bboxes[:, 3] > bboxes[:, 1])
            # If the crop does not contain any gt-bbox area and
            # self.allow_negative_crop is False, skip this image.
            if (key == 'gt_bboxes' and not valid_inds.any()
                    and not self.allow_negative_crop):
                return None
            results[key] = bboxes[valid_inds, :]
            # label fields. e.g. gt_labels and gt_labels_ignore
            label_key = self.bbox2label.get(key)
            if label_key in results:
                results[label_key] = results[label_key][valid_inds]

            # mask fields, e.g. gt_masks and gt_masks_ignore
            mask_key = self.bbox2mask.get(key)
            if mask_key in results:
                results[mask_key] = results[mask_key][
                    valid_inds.nonzero()[0]].crop(
                        np.asarray([crop_x1, crop_y1, crop_x2, crop_y2]))

        # crop semantic seg
        for key in results.get('seg_fields', []):
            results[key] = results[key][crop_y1:crop_y2, crop_x1:crop_x2]

        return results

    def __repr__(self):
        return self.__class__.__name__ + f'(crop_size={self.crop_size})'


@PIPELINES.register_module()
class SegRescale(object):
    """Rescale semantic segmentation maps.

    Args:
        scale_factor (float): The scale factor of the final output.
        backend (str): Image rescale backend, choices are 'cv2' and 'pillow'.
            These two backends generates slightly different results. Defaults
            to 'cv2'.
    """

    def __init__(self, scale_factor=1, backend='cv2'):
        self.scale_factor = scale_factor
        self.backend = backend

    def __call__(self, results):
        """Call function to scale the semantic segmentation map.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with semantic segmentation map scaled.
        """

        for key in results.get('seg_fields', []):
            if self.scale_factor != 1:
                results[key] = mmcv.imrescale(
                    results[key],
                    self.scale_factor,
                    interpolation='nearest',
                    backend=self.backend)
        return results

    def __repr__(self):
        return self.__class__.__name__ + f'(scale_factor={self.scale_factor})'


@PIPELINES.register_module()
class PhotoMetricDistortion(object):
    """Apply photometric distortion to image sequentially, every transformation
    is applied with a probability of 0.5. The position of random contrast is in
    second or second to last.

    1. random brightness
    2. random contrast (mode 0)
    3. convert color from BGR to HSV
    4. random saturation
    5. random hue
    6. convert color from HSV to BGR
    7. random contrast (mode 1)
    8. randomly swap channels

    Args:
        brightness_delta (int): delta of brightness.
        contrast_range (tuple): range of contrast.
        saturation_range (tuple): range of saturation.
        hue_delta (int): delta of hue.
    """

    def __init__(self,
                 brightness_delta=32,
                 contrast_range=(0.5, 1.5),
                 saturation_range=(0.5, 1.5),
                 hue_delta=18):
        self.brightness_delta = brightness_delta
        self.contrast_lower, self.contrast_upper = contrast_range
        self.saturation_lower, self.saturation_upper = saturation_range
        self.hue_delta = hue_delta

    def __call__(self, results):
        """Call function to perform photometric distortion on images.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images distorted.
        """

        if 'img_fields' in results:
            assert results['img_fields'] == ['img'], \
                'Only single img_fields is allowed'
        img = results['img']
        assert img.dtype == np.float32, \
            'PhotoMetricDistortion needs the input image of dtype np.float32,'\
            ' please set "to_float32=True" in "LoadImageFromFile" pipeline'
        # random brightness
        if random.randint(2):
            delta = random.uniform(-self.brightness_delta,
                                   self.brightness_delta)
            img += delta

        # mode == 0 --> do random contrast first
        # mode == 1 --> do random contrast last
        mode = random.randint(2)
        if mode == 1:
            if random.randint(2):
                alpha = random.uniform(self.contrast_lower,
                                       self.contrast_upper)
                img *= alpha

        # convert color from BGR to HSV
        img = mmcv.bgr2hsv(img)

        # random saturation
        if random.randint(2):
            img[..., 1] *= random.uniform(self.saturation_lower,
                                          self.saturation_upper)

        # random hue
        if random.randint(2):
            img[..., 0] += random.uniform(-self.hue_delta, self.hue_delta)
            img[..., 0][img[..., 0] > 360] -= 360
            img[..., 0][img[..., 0] < 0] += 360

        # convert color from HSV to BGR
        img = mmcv.hsv2bgr(img)

        # random contrast
        if mode == 0:
            if random.randint(2):
                alpha = random.uniform(self.contrast_lower,
                                       self.contrast_upper)
                img *= alpha

        # randomly swap channels
        if random.randint(2):
            img = img[..., random.permutation(3)]

        results['img'] = img
        results['img_shape'] = img.shape
        results['ori_shape'] = img.shape
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(\nbrightness_delta={self.brightness_delta},\n'
        repr_str += 'contrast_range='
        repr_str += f'{(self.contrast_lower, self.contrast_upper)},\n'
        repr_str += 'saturation_range='
        repr_str += f'{(self.saturation_lower, self.saturation_upper)},\n'
        repr_str += f'hue_delta={self.hue_delta})'
        return repr_str


@PIPELINES.register_module()
class Expand(object):
    """Random expand the image & bboxes.

    Randomly place the original image on a canvas of 'ratio' x original image
    size filled with mean values. The ratio is in the range of ratio_range.

    Args:
        mean (tuple): mean value of dataset.
        to_rgb (bool): if need to convert the order of mean to align with RGB.
        ratio_range (tuple): range of expand ratio.
        prob (float): probability of applying this transformation
    """

    def __init__(self,
                 mean=(0, 0, 0),
                 to_rgb=True,
                 ratio_range=(1, 4),
                 seg_ignore_label=None,
                 prob=0.5):
        self.to_rgb = to_rgb
        self.ratio_range = ratio_range
        if to_rgb:
            self.mean = mean[::-1]
        else:
            self.mean = mean
        self.min_ratio, self.max_ratio = ratio_range
        self.seg_ignore_label = seg_ignore_label
        self.prob = prob

    def __call__(self, results):
        """Call function to expand images, bounding boxes.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images, bounding boxes expanded
        """

        if random.uniform(0, 1) > self.prob:
            return results

        if 'img_fields' in results:
            assert results['img_fields'] == ['img'], \
                'Only single img_fields is allowed'
        img = results['img']

        h, w, c = img.shape
        ratio = random.uniform(self.min_ratio, self.max_ratio)
        expand_img = np.full((int(h * ratio), int(w * ratio), c),
                             self.mean,
                             dtype=img.dtype)
        left = int(random.uniform(0, w * ratio - w))
        top = int(random.uniform(0, h * ratio - h))
        expand_img[top:top + h, left:left + w] = img

        results['img'] = expand_img
        # expand bboxes
        for key in results.get('bbox_fields', []):
            results[key] = results[key] + np.tile(
                (left, top), 2).astype(results[key].dtype)

        # expand masks
        for key in results.get('mask_fields', []):
            results[key] = results[key].expand(
                int(h * ratio), int(w * ratio), top, left)

        # expand segs
        for key in results.get('seg_fields', []):
            gt_seg = results[key]
            expand_gt_seg = np.full((int(h * ratio), int(w * ratio)),
                                    self.seg_ignore_label,
                                    dtype=gt_seg.dtype)
            expand_gt_seg[top:top + h, left:left + w] = gt_seg
            results[key] = expand_gt_seg
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(mean={self.mean}, to_rgb={self.to_rgb}, '
        repr_str += f'ratio_range={self.ratio_range}, '
        repr_str += f'seg_ignore_label={self.seg_ignore_label})'
        return repr_str


@PIPELINES.register_module()
class MinIoURandomCrop(object):
    """Random crop the image & bboxes, the cropped patches have minimum IoU
    requirement with original image & bboxes, the IoU threshold is randomly
    selected from min_ious.

    Args:
        min_ious (tuple): minimum IoU threshold for all intersections with
        bounding boxes
        min_crop_size (float): minimum crop's size (i.e. h,w := a*h, a*w,
        where a >= min_crop_size).

    Note:
        The keys for bboxes, labels and masks should be paired. That is, \
        `gt_bboxes` corresponds to `gt_labels` and `gt_masks`, and \
        `gt_bboxes_ignore` to `gt_labels_ignore` and `gt_masks_ignore`.
    """

    def __init__(self, min_ious=(0.1, 0.3, 0.5, 0.7, 0.9), min_crop_size=0.3):
        # 1: return ori img
        self.min_ious = min_ious
        self.sample_mode = (1, *min_ious, 0)
        self.min_crop_size = min_crop_size
        self.bbox2label = {
            'gt_bboxes': 'gt_labels',
            'gt_bboxes_ignore': 'gt_labels_ignore'
        }
        self.bbox2mask = {
            'gt_bboxes': 'gt_masks',
            'gt_bboxes_ignore': 'gt_masks_ignore'
        }

    def __call__(self, results):
        """Call function to crop images and bounding boxes with minimum IoU
        constraint.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images and bounding boxes cropped, \
                'img_shape' key is updated.
        """

        if 'img_fields' in results:
            assert results['img_fields'] == ['img'], \
                'Only single img_fields is allowed'
        img = results['img']
        assert 'bbox_fields' in results
        boxes = [results[key] for key in results['bbox_fields']]
        boxes = np.concatenate(boxes, 0)
        h, w, c = img.shape
        while True:
            mode = random.choice(self.sample_mode)
            self.mode = mode
            if mode == 1:
                return results

            min_iou = mode
            for i in range(50):
                new_w = random.uniform(self.min_crop_size * w, w)
                new_h = random.uniform(self.min_crop_size * h, h)

                # h / w in [0.5, 2]
                if new_h / new_w < 0.5 or new_h / new_w > 2:
                    continue

                left = random.uniform(w - new_w)
                top = random.uniform(h - new_h)

                patch = np.array(
                    (int(left), int(top), int(left + new_w), int(top + new_h)))
                # Line or point crop is not allowed
                if patch[2] == patch[0] or patch[3] == patch[1]:
                    continue
                overlaps = bbox_overlaps(
                    patch.reshape(-1, 4), boxes.reshape(-1, 4)).reshape(-1)
                if len(overlaps) > 0 and overlaps.min() < min_iou:
                    continue

                # center of boxes should inside the crop img
                # only adjust boxes and instance masks when the gt is not empty
                if len(overlaps) > 0:
                    # adjust boxes
                    def is_center_of_bboxes_in_patch(boxes, patch):
                        center = (boxes[:, :2] + boxes[:, 2:]) / 2
                        mask = ((center[:, 0] > patch[0]) *
                                (center[:, 1] > patch[1]) *
                                (center[:, 0] < patch[2]) *
                                (center[:, 1] < patch[3]))
                        return mask

                    mask = is_center_of_bboxes_in_patch(boxes, patch)
                    if not mask.any():
                        continue
                    for key in results.get('bbox_fields', []):
                        boxes = results[key].copy()
                        mask = is_center_of_bboxes_in_patch(boxes, patch)
                        boxes = boxes[mask]
                        boxes[:, 2:] = boxes[:, 2:].clip(max=patch[2:])
                        boxes[:, :2] = boxes[:, :2].clip(min=patch[:2])
                        boxes -= np.tile(patch[:2], 2)

                        results[key] = boxes
                        # labels
                        label_key = self.bbox2label.get(key)
                        if label_key in results:
                            results[label_key] = results[label_key][mask]

                        # mask fields
                        mask_key = self.bbox2mask.get(key)
                        if mask_key in results:
                            results[mask_key] = results[mask_key][
                                mask.nonzero()[0]].crop(patch)
                # adjust the img no matter whether the gt is empty before crop
                img = img[patch[1]:patch[3], patch[0]:patch[2]]
                results['img'] = img
                results['img_shape'] = img.shape

                # seg fields
                for key in results.get('seg_fields', []):
                    results[key] = results[key][patch[1]:patch[3],
                                                patch[0]:patch[2]]
                return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(min_ious={self.min_ious}, '
        repr_str += f'min_crop_size={self.min_crop_size})'
        return repr_str


@PIPELINES.register_module()
class Corrupt(object):
    """Corruption augmentation.

    Corruption transforms implemented based on
    `imagecorruptions <https://github.com/bethgelab/imagecorruptions>`_.

    Args:
        corruption (str): Corruption name.
        severity (int, optional): The severity of corruption. Default: 1.
    """

    def __init__(self, corruption, severity=1):
        self.corruption = corruption
        self.severity = severity

    def __call__(self, results):
        """Call function to corrupt image.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with images corrupted.
        """

        if corrupt is None:
            raise RuntimeError('imagecorruptions is not installed')
        if 'img_fields' in results:
            assert results['img_fields'] == ['img'], \
                'Only single img_fields is allowed'
        results['img'] = corrupt(
            results['img'].astype(np.uint8),
            corruption_name=self.corruption,
            severity=self.severity)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(corruption={self.corruption}, '
        repr_str += f'severity={self.severity})'
        return repr_str


@PIPELINES.register_module()
class Albu(object):
    """Albumentation augmentation.

    Adds custom transformations from Albumentations library.
    Please, visit `https://albumentations.readthedocs.io`
    to get more information.

    An example of ``transforms`` is as followed:

    .. code-block::

        [
            dict(
                type='ShiftScaleRotate',
                shift_limit=0.0625,
                scale_limit=0.0,
                rotate_limit=0,
                interpolation=1,
                p=0.5),
            dict(
                type='RandomBrightnessContrast',
                brightness_limit=[0.1, 0.3],
                contrast_limit=[0.1, 0.3],
                p=0.2),
            dict(type='ChannelShuffle', p=0.1),
            dict(
                type='OneOf',
                transforms=[
                    dict(type='Blur', blur_limit=3, p=1.0),
                    dict(type='MedianBlur', blur_limit=3, p=1.0)
                ],
                p=0.1),
        ]

    Args:
        transforms (list[dict]): A list of albu transformations
        bbox_params (dict): Bbox_params for albumentation `Compose`
        keymap (dict): Contains {'input key':'albumentation-style key'}
        skip_img_without_anno (bool): Whether to skip the image if no ann left
            after aug
    """

    def __init__(self,
                 transforms,
                 bbox_params=None,
                 keymap=None,
                 update_pad_shape=False,
                 skip_img_without_anno=False):
        if Compose is None:
            raise RuntimeError('albumentations is not installed')

        self.transforms = transforms
        self.filter_lost_elements = False
        self.update_pad_shape = update_pad_shape
        self.skip_img_without_anno = skip_img_without_anno

        # A simple workaround to remove masks without boxes
        if (isinstance(bbox_params, dict) and 'label_fields' in bbox_params
                and 'filter_lost_elements' in bbox_params):
            self.filter_lost_elements = True
            self.origin_label_fields = bbox_params['label_fields']
            bbox_params['label_fields'] = ['idx_mapper']
            del bbox_params['filter_lost_elements']

        self.bbox_params = (
            self.albu_builder(bbox_params) if bbox_params else None)
        self.aug = Compose([self.albu_builder(t) for t in self.transforms],
                           bbox_params=self.bbox_params)

        if not keymap:
            self.keymap_to_albu = {
                'img': 'image',
                'gt_masks': 'masks',
                'gt_bboxes': 'bboxes'
            }
        else:
            self.keymap_to_albu = keymap
        self.keymap_back = {v: k for k, v in self.keymap_to_albu.items()}

    def albu_builder(self, cfg):
        """Import a module from albumentations.

        It inherits some of :func:`build_from_cfg` logic.

        Args:
            cfg (dict): Config dict. It should at least contain the key "type".

        Returns:
            obj: The constructed object.
        """

        assert isinstance(cfg, dict) and 'type' in cfg
        args = cfg.copy()

        obj_type = args.pop('type')
        if mmcv.is_str(obj_type):
            if albumentations is None:
                raise RuntimeError('albumentations is not installed')
            obj_cls = getattr(albumentations, obj_type)
        elif inspect.isclass(obj_type):
            obj_cls = obj_type
        else:
            raise TypeError(
                f'type must be a str or valid type, but got {type(obj_type)}')

        if 'transforms' in args:
            args['transforms'] = [
                self.albu_builder(transform)
                for transform in args['transforms']
            ]

        return obj_cls(**args)

    @staticmethod
    def mapper(d, keymap):
        """Dictionary mapper. Renames keys according to keymap provided.

        Args:
            d (dict): old dict
            keymap (dict): {'old_key':'new_key'}
        Returns:
            dict: new dict.
        """

        updated_dict = {}
        for k, v in zip(d.keys(), d.values()):
            new_k = keymap.get(k, k)
            updated_dict[new_k] = d[k]
        return updated_dict

    def __call__(self, results):
        # dict to albumentations format
        results = self.mapper(results, self.keymap_to_albu)
        # TODO: add bbox_fields
        if 'bboxes' in results:
            # to list of boxes
            if isinstance(results['bboxes'], np.ndarray):
                results['bboxes'] = [x for x in results['bboxes']]
            # add pseudo-field for filtration
            if self.filter_lost_elements:
                results['idx_mapper'] = np.arange(len(results['bboxes']))

        # TODO: Support mask structure in albu
        if 'masks' in results:
            if isinstance(results['masks'], PolygonMasks):
                raise NotImplementedError(
                    'Albu only supports BitMap masks now')
            ori_masks = results['masks']
            results['masks'] = results['masks'].masks

        results = self.aug(**results)

        if 'bboxes' in results:
            if isinstance(results['bboxes'], list):
                results['bboxes'] = np.array(
                    results['bboxes'], dtype=np.float32)
            results['bboxes'] = results['bboxes'].reshape(-1, 4)

            # filter label_fields
            if self.filter_lost_elements:

                for label in self.origin_label_fields:
                    results[label] = np.array(
                        [results[label][i] for i in results['idx_mapper']])
                if 'masks' in results:
                    results['masks'] = np.array(
                        [results['masks'][i] for i in results['idx_mapper']])
                    results['masks'] = ori_masks.__class__(
                        results['masks'], results['image'].shape[0],
                        results['image'].shape[1])

                if (not len(results['idx_mapper'])
                        and self.skip_img_without_anno):
                    return None

        if 'gt_labels' in results:
            if isinstance(results['gt_labels'], list):
                results['gt_labels'] = np.array(results['gt_labels'])
            results['gt_labels'] = results['gt_labels'].astype(np.int64)

        # back to the original format
        results = self.mapper(results, self.keymap_back)

        # update final shape
        if self.update_pad_shape:
            results['pad_shape'] = results['img'].shape

        return results

    def __repr__(self):
        repr_str = self.__class__.__name__ + f'(transforms={self.transforms})'
        return repr_str


@PIPELINES.register_module()
class RandomCenterCropPad(object):
    """Random center crop and random around padding for CornerNet.

    This operation generates randomly cropped image from the original image and
    pads it simultaneously. Different from :class:`RandomCrop`, the output
    shape may not equal to ``crop_size`` strictly. We choose a random value
    from ``ratios`` and the output shape could be larger or smaller than
    ``crop_size``. The padding operation is also different from :class:`Pad`,
    here we use around padding instead of right-bottom padding.

    The relation between output image (padding image) and original image:

    .. code:: text

                        output image

               +----------------------------+
               |          padded area       |
        +------|----------------------------|----------+
        |      |         cropped area       |          |
        |      |         +---------------+  |          |
        |      |         |    .   center |  |          | original image
        |      |         |        range  |  |          |
        |      |         +---------------+  |          |
        +------|----------------------------|----------+
               |          padded area       |
               +----------------------------+

    There are 5 main areas in the figure:

    - output image: output image of this operation, also called padding
      image in following instruction.
    - original image: input image of this operation.
    - padded area: non-intersect area of output image and original image.
    - cropped area: the overlap of output image and original image.
    - center range: a smaller area where random center chosen from.
      center range is computed by ``border`` and original image's shape
      to avoid our random center is too close to original image's border.

    Also this operation act differently in train and test mode, the summary
    pipeline is listed below.

    Train pipeline:

    1. Choose a ``random_ratio`` from ``ratios``, the shape of padding image
       will be ``random_ratio * crop_size``.
    2. Choose a ``random_center`` in center range.
    3. Generate padding image with center matches the ``random_center``.
    4. Initialize the padding image with pixel value equals to ``mean``.
    5. Copy the cropped area to padding image.
    6. Refine annotations.

    Test pipeline:

    1. Compute output shape according to ``test_pad_mode``.
    2. Generate padding image with center matches the original image
       center.
    3. Initialize the padding image with pixel value equals to ``mean``.
    4. Copy the ``cropped area`` to padding image.

    Args:
        crop_size (tuple | None): expected size after crop, final size will
            computed according to ratio. Requires (h, w) in train mode, and
            None in test mode.
        ratios (tuple): random select a ratio from tuple and crop image to
            (crop_size[0] * ratio) * (crop_size[1] * ratio).
            Only available in train mode.
        border (int): max distance from center select area to image border.
            Only available in train mode.
        mean (sequence): Mean values of 3 channels.
        std (sequence): Std values of 3 channels.
        to_rgb (bool): Whether to convert the image from BGR to RGB.
        test_mode (bool): whether involve random variables in transform.
            In train mode, crop_size is fixed, center coords and ratio is
            random selected from predefined lists. In test mode, crop_size
            is image's original shape, center coords and ratio is fixed.
        test_pad_mode (tuple): padding method and padding shape value, only
            available in test mode. Default is using 'logical_or' with
            127 as padding shape value.

            - 'logical_or': final_shape = input_shape | padding_shape_value
            - 'size_divisor': final_shape = int(
              ceil(input_shape / padding_shape_value) * padding_shape_value)
    """

    def __init__(self,
                 crop_size=None,
                 ratios=(0.9, 1.0, 1.1),
                 border=128,
                 mean=None,
                 std=None,
                 to_rgb=None,
                 test_mode=False,
                 test_pad_mode=('logical_or', 127)):
        if test_mode:
            assert crop_size is None, 'crop_size must be None in test mode'
            assert ratios is None, 'ratios must be None in test mode'
            assert border is None, 'border must be None in test mode'
            assert isinstance(test_pad_mode, (list, tuple))
            assert test_pad_mode[0] in ['logical_or', 'size_divisor']
        else:
            assert isinstance(crop_size, (list, tuple))
            assert crop_size[0] > 0 and crop_size[1] > 0, (
                'crop_size must > 0 in train mode')
            assert isinstance(ratios, (list, tuple))
            assert test_pad_mode is None, (
                'test_pad_mode must be None in train mode')

        self.crop_size = crop_size
        self.ratios = ratios
        self.border = border
        # We do not set default value to mean, std and to_rgb because these
        # hyper-parameters are easy to forget but could affect the performance.
        # Please use the same setting as Normalize for performance assurance.
        assert mean is not None and std is not None and to_rgb is not None
        self.to_rgb = to_rgb
        self.input_mean = mean
        self.input_std = std
        if to_rgb:
            self.mean = mean[::-1]
            self.std = std[::-1]
        else:
            self.mean = mean
            self.std = std
        self.test_mode = test_mode
        self.test_pad_mode = test_pad_mode

    def _get_border(self, border, size):
        """Get final border for the target size.

        This function generates a ``final_border`` according to image's shape.
        The area between ``final_border`` and ``size - final_border`` is the
        ``center range``. We randomly choose center from the ``center range``
        to avoid our random center is too close to original image's border.
        Also ``center range`` should be larger than 0.

        Args:
            border (int): The initial border, default is 128.
            size (int): The width or height of original image.
        Returns:
            int: The final border.
        """
        k = 2 * border / size
        i = pow(2, np.ceil(np.log2(np.ceil(k))) + (k == int(k)))
        return border // i

    def _filter_boxes(self, patch, boxes):
        """Check whether the center of each box is in the patch.

        Args:
            patch (list[int]): The cropped area, [left, top, right, bottom].
            boxes (numpy array, (N x 4)): Ground truth boxes.

        Returns:
            mask (numpy array, (N,)): Each box is inside or outside the patch.
        """
        center = (boxes[:, :2] + boxes[:, 2:]) / 2
        mask = (center[:, 0] > patch[0]) * (center[:, 1] > patch[1]) * (
            center[:, 0] < patch[2]) * (
                center[:, 1] < patch[3])
        return mask

    def _crop_image_and_paste(self, image, center, size):
        """Crop image with a given center and size, then paste the cropped
        image to a blank image with two centers align.

        This function is equivalent to generating a blank image with ``size``
        as its shape. Then cover it on the original image with two centers (
        the center of blank image and the random center of original image)
        aligned. The overlap area is paste from the original image and the
        outside area is filled with ``mean pixel``.

        Args:
            image (np array, H x W x C): Original image.
            center (list[int]): Target crop center coord.
            size (list[int]): Target crop size. [target_h, target_w]

        Returns:
            cropped_img (np array, target_h x target_w x C): Cropped image.
            border (np array, 4): The distance of four border of
                ``cropped_img`` to the original image area, [top, bottom,
                left, right]
            patch (list[int]): The cropped area, [left, top, right, bottom].
        """
        center_y, center_x = center
        target_h, target_w = size
        img_h, img_w, img_c = image.shape

        x0 = max(0, center_x - target_w // 2)
        x1 = min(center_x + target_w // 2, img_w)
        y0 = max(0, center_y - target_h // 2)
        y1 = min(center_y + target_h // 2, img_h)
        patch = np.array((int(x0), int(y0), int(x1), int(y1)))

        left, right = center_x - x0, x1 - center_x
        top, bottom = center_y - y0, y1 - center_y

        cropped_center_y, cropped_center_x = target_h // 2, target_w // 2
        cropped_img = np.zeros((target_h, target_w, img_c), dtype=image.dtype)
        for i in range(img_c):
            cropped_img[:, :, i] += self.mean[i]
        y_slice = slice(cropped_center_y - top, cropped_center_y + bottom)
        x_slice = slice(cropped_center_x - left, cropped_center_x + right)
        cropped_img[y_slice, x_slice, :] = image[y0:y1, x0:x1, :]

        border = np.array([
            cropped_center_y - top, cropped_center_y + bottom,
            cropped_center_x - left, cropped_center_x + right
        ],
                          dtype=np.float32)

        return cropped_img, border, patch

    def _train_aug(self, results):
        """Random crop and around padding the original image.

        Args:
            results (dict): Image infomations in the augment pipeline.

        Returns:
            results (dict): The updated dict.
        """
        img = results['img']
        h, w, c = img.shape
        boxes = results['gt_bboxes']
        while True:
            scale = random.choice(self.ratios)
            new_h = int(self.crop_size[0] * scale)
            new_w = int(self.crop_size[1] * scale)
            h_border = self._get_border(self.border, h)
            w_border = self._get_border(self.border, w)

            for i in range(50):
                center_x = random.randint(low=w_border, high=w - w_border)
                center_y = random.randint(low=h_border, high=h - h_border)

                cropped_img, border, patch = self._crop_image_and_paste(
                    img, [center_y, center_x], [new_h, new_w])

                mask = self._filter_boxes(patch, boxes)
                # if image do not have valid bbox, any crop patch is valid.
                if not mask.any() and len(boxes) > 0:
                    continue

                results['img'] = cropped_img
                results['img_shape'] = cropped_img.shape
                results['pad_shape'] = cropped_img.shape

                x0, y0, x1, y1 = patch

                left_w, top_h = center_x - x0, center_y - y0
                cropped_center_x, cropped_center_y = new_w // 2, new_h // 2

                # crop bboxes accordingly and clip to the image boundary
                for key in results.get('bbox_fields', []):
                    mask = self._filter_boxes(patch, results[key])
                    bboxes = results[key][mask]
                    bboxes[:, 0:4:2] += cropped_center_x - left_w - x0
                    bboxes[:, 1:4:2] += cropped_center_y - top_h - y0
                    bboxes[:, 0:4:2] = np.clip(bboxes[:, 0:4:2], 0, new_w)
                    bboxes[:, 1:4:2] = np.clip(bboxes[:, 1:4:2], 0, new_h)
                    keep = (bboxes[:, 2] > bboxes[:, 0]) & (
                        bboxes[:, 3] > bboxes[:, 1])
                    bboxes = bboxes[keep]
                    results[key] = bboxes
                    if key in ['gt_bboxes']:
                        if 'gt_labels' in results:
                            labels = results['gt_labels'][mask]
                            labels = labels[keep]
                            results['gt_labels'] = labels
                        if 'gt_masks' in results:
                            raise NotImplementedError(
                                'RandomCenterCropPad only supports bbox.')

                # crop semantic seg
                for key in results.get('seg_fields', []):
                    raise NotImplementedError(
                        'RandomCenterCropPad only supports bbox.')
                return results

    def _test_aug(self, results):
        """Around padding the original image without cropping.

        The padding mode and value are from ``test_pad_mode``.

        Args:
            results (dict): Image infomations in the augment pipeline.

        Returns:
            results (dict): The updated dict.
        """
        img = results['img']
        h, w, c = img.shape
        results['img_shape'] = img.shape
        if self.test_pad_mode[0] in ['logical_or']:
            target_h = h | self.test_pad_mode[1]
            target_w = w | self.test_pad_mode[1]
        elif self.test_pad_mode[0] in ['size_divisor']:
            divisor = self.test_pad_mode[1]
            target_h = int(np.ceil(h / divisor)) * divisor
            target_w = int(np.ceil(w / divisor)) * divisor
        else:
            raise NotImplementedError(
                'RandomCenterCropPad only support two testing pad mode:'
                'logical-or and size_divisor.')

        cropped_img, border, _ = self._crop_image_and_paste(
            img, [h // 2, w // 2], [target_h, target_w])
        results['img'] = cropped_img
        results['pad_shape'] = cropped_img.shape
        results['border'] = border
        return results

    def __call__(self, results):
        img = results['img']
        assert img.dtype == np.float32, (
            'RandomCenterCropPad needs the input image of dtype np.float32,'
            ' please set "to_float32=True" in "LoadImageFromFile" pipeline')
        h, w, c = img.shape
        assert c == len(self.mean)
        if self.test_mode:
            return self._test_aug(results)
        else:
            return self._train_aug(results)

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(crop_size={self.crop_size}, '
        repr_str += f'ratios={self.ratios}, '
        repr_str += f'border={self.border}, '
        repr_str += f'mean={self.input_mean}, '
        repr_str += f'std={self.input_std}, '
        repr_str += f'to_rgb={self.to_rgb}, '
        repr_str += f'test_mode={self.test_mode}, '
        repr_str += f'test_pad_mode={self.test_pad_mode})'
        return repr_str


@PIPELINES.register_module()
class CutOut(object):
    """CutOut operation.

    Randomly drop some regions of image used in
    `Cutout <https://arxiv.org/abs/1708.04552>`_.

    Args:
        n_holes (int | tuple[int, int]): Number of regions to be dropped.
            If it is given as a list, number of holes will be randomly
            selected from the closed interval [`n_holes[0]`, `n_holes[1]`].
        cutout_shape (tuple[int, int] | list[tuple[int, int]]): The candidate
            shape of dropped regions. It can be `tuple[int, int]` to use a
            fixed cutout shape, or `list[tuple[int, int]]` to randomly choose
            shape from the list.
        cutout_ratio (tuple[float, float] | list[tuple[float, float]]): The
            candidate ratio of dropped regions. It can be `tuple[float, float]`
            to use a fixed ratio or `list[tuple[float, float]]` to randomly
            choose ratio from the list. Please note that `cutout_shape`
            and `cutout_ratio` cannot be both given at the same time.
        fill_in (tuple[float, float, float] | tuple[int, int, int]): The value
            of pixel to fill in the dropped regions. Default: (0, 0, 0).
    """

    def __init__(self,
                 n_holes,
                 cutout_shape=None,
                 cutout_ratio=None,
                 fill_in=(0, 0, 0)):

        assert (cutout_shape is None) ^ (cutout_ratio is None), \
            'Either cutout_shape or cutout_ratio should be specified.'
        assert (isinstance(cutout_shape, (list, tuple))
                or isinstance(cutout_ratio, (list, tuple)))
        if isinstance(n_holes, tuple):
            assert len(n_holes) == 2 and 0 <= n_holes[0] < n_holes[1]
        else:
            n_holes = (n_holes, n_holes)
        self.n_holes = n_holes
        self.fill_in = fill_in
        self.with_ratio = cutout_ratio is not None
        self.candidates = cutout_ratio if self.with_ratio else cutout_shape
        if not isinstance(self.candidates, list):
            self.candidates = [self.candidates]

    def __call__(self, results):
        """Call function to drop some regions of image."""
        h, w, c = results['img'].shape
        n_holes = np.random.randint(self.n_holes[0], self.n_holes[1] + 1)
        for _ in range(n_holes):
            x1 = np.random.randint(0, w)
            y1 = np.random.randint(0, h)
            index = np.random.randint(0, len(self.candidates))
            if not self.with_ratio:
                cutout_w, cutout_h = self.candidates[index]
            else:
                cutout_w = int(self.candidates[index][0] * w)
                cutout_h = int(self.candidates[index][1] * h)

            x2 = np.clip(x1 + cutout_w, 0, w)
            y2 = np.clip(y1 + cutout_h, 0, h)
            results['img'][y1:y2, x1:x2, :] = self.fill_in

        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(n_holes={self.n_holes}, '
        repr_str += (f'cutout_ratio={self.candidates}, ' if self.with_ratio
                     else f'cutout_shape={self.candidates}, ')
        repr_str += f'fill_in={self.fill_in})'
        return repr_str


@PIPELINES.register_module()
class SoftGridMask(object):
    """SoftGridMask augmentation.

    This augmentation is an extension of
    `GridMask Data Augmentation <https://arxiv.org/abs/2001.04086>`_.
    Unlike the hard (bool value) mask of the original GridMask,
    various soft (float value) mask patterns can be specified by mask_pattern.

    References:
        https://github.com/Jia-Research-Lab/GridMask/blob/master/detection_grid/maskrcnn_benchmark/data/transforms/grid.py
        https://github.com/albumentations-team/albumentations/blob/master/albumentations/augmentations/transforms.py
    """

    def __init__(self,
                 ratio=0.5,
                 ratio_h=None,
                 ratio_w=None,
                 unit_range=(2, 100000),
                 unit_h_range=None,
                 unit_w_range=None,
                 mask_pattern=((1.0, 1.0), (1.0, 0.0)),
                 mask_pattern_max=None,
                 square_unit=True,
                 prob=0.7):
        assert (ratio is None) ^ (ratio_h is None)
        assert (ratio is None) ^ (ratio_w is None)
        if ratio:
            ratio_h = ratio_w = ratio
        assert 0.0 <= ratio_h <= 1.0
        assert 0.0 <= ratio_w <= 1.0
        self.ratio_h = ratio_h
        self.ratio_w = ratio_w

        assert (unit_range is None) ^ (unit_h_range is None)
        assert (unit_range is None) ^ (unit_w_range is None)
        if unit_range:
            unit_h_range = unit_w_range = unit_range
        assert isinstance(unit_h_range, tuple) and len(unit_h_range) == 2
        assert isinstance(unit_w_range, tuple) and len(unit_w_range) == 2
        self.unit_h_range = unit_h_range
        self.unit_w_range = unit_w_range
        self.unit_h_min, self.unit_h_max = unit_h_range
        self.unit_w_min, self.unit_w_max = unit_w_range
        assert 2 <= self.unit_h_min <= self.unit_h_max
        assert 2 <= self.unit_w_min <= self.unit_w_max

        mask_pattern = np.array(mask_pattern)
        assert mask_pattern.shape == (2, 2), \
            'Only 2x2 pattern is supported currently.'
        if mask_pattern_max is not None:
            mask_pattern_max = np.array(mask_pattern_max)
            assert mask_pattern_max.shape == (2, 2), \
                'Only 2x2 pattern is supported currently.'
        self.mask_pattern = mask_pattern
        self.mask_pattern_max = mask_pattern_max

        assert 0.0 <= prob <= 1.0
        self.square_unit = square_unit
        self.prob = prob

    # @imwrite_denormalized_debug_img
    def __call__(self, results):
        """Call function to perform SoftGridMask augmentation on images.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Result dict with grid-masked images.
        """

        if random.uniform(0, 1) > self.prob:
            return results
        if len(results.get('mask_fields', [])) > 0:
            raise NotImplementedError

        img_h, img_w = results['img'].shape[:2]
        unit_h_max = min(img_h, img_w, self.unit_h_max)
        unit_h = random.randint(self.unit_h_min, unit_h_max + 1)
        if self.square_unit:
            unit_w = unit_h
        else:
            unit_w_max = min(img_h, img_w, self.unit_w_max)
            unit_w = random.randint(self.unit_w_min, unit_w_max + 1)
        shift_y = random.randint(unit_h)
        shift_x = random.randint(unit_w)

        # calculate border position
        # each grid should be at least 1 pixel
        border_h = int(unit_h * self.ratio_h + 0.5)
        border_w = int(unit_w * self.ratio_w + 0.5)
        border_h = min(max(border_h, 1), unit_h - 1)
        border_w = min(max(border_w, 1), unit_w - 1)

        if self.mask_pattern_max is not None:
            mask_pattern = random.uniform(self.mask_pattern,
                                          self.mask_pattern_max)
        else:
            mask_pattern = self.mask_pattern

        # prepare mask by tiling unit
        unit_mask = np.ones((unit_h, unit_w), dtype=np.float32)
        unit_mask[:border_h, :border_w] = mask_pattern[0, 0]
        unit_mask[:border_h, border_w:] = mask_pattern[0, 1]
        unit_mask[border_h:, :border_w] = mask_pattern[1, 0]
        unit_mask[border_h:, border_w:] = mask_pattern[1, 1]
        repeat_h = img_h // unit_h + 2  # +1 for ceil, +1 for shift
        repeat_w = img_w // unit_w + 2  # +1 for ceil, +1 for shift
        mask = np.tile(unit_mask, (repeat_h, repeat_w))

        # crop mask with shift
        mask = mask[shift_y:shift_y + img_h, shift_x:shift_x + img_w]

        results['img'] *= mask[:, :, np.newaxis]
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(ratio_h={self.ratio_h}, '
        repr_str += f'ratio_w={self.ratio_w}, '
        repr_str += f'unit_h_range={self.unit_h_range}, '
        repr_str += f'unit_w_range={self.unit_w_range}, '
        repr_str += f'mask_pattern={self.mask_pattern}, '
        repr_str += f'mask_pattern_max={self.mask_pattern_max}, '
        repr_str += f'square_unit={self.square_unit}, '
        repr_str += f'prob={self.prob})'
        return repr_str


@PIPELINES.register_module()
class AutoAugment(object):
    def __init__(self, autoaug_type="v1"):
        """
        Args:
            autoaug_type (str): autoaug type, support v0, v1, v2, v3, test
        """
        super(AutoAugment, self).__init__()
        self.autoaug_type = autoaug_type

    def __call__(self, results):
        """
        Learning Data Augmentation Strategies for Object Detection, see https://arxiv.org/abs/1906.11172
        """
        gt_bbox = results['gt_bboxes']
        im = results['img']
        if len(gt_bbox) == 0:
            return results

        # gt_boxes : [x1, y1, x2, y2]
        # norm_gt_boxes: [y1, x1, y2, x2]
        height, width, _ = im.shape
        norm_gt_bbox = np.ones_like(gt_bbox, dtype=np.float32)
        norm_gt_bbox[:, 0] = gt_bbox[:, 1] / float(height)
        norm_gt_bbox[:, 1] = gt_bbox[:, 0] / float(width)
        norm_gt_bbox[:, 2] = gt_bbox[:, 3] / float(height)
        norm_gt_bbox[:, 3] = gt_bbox[:, 2] / float(width)

        from mmdet.datasets.pipelines.autoaugment_utils import distort_image_with_autoaugment
        im, norm_gt_bbox = distort_image_with_autoaugment(im, norm_gt_bbox,
                                                          self.autoaug_type)
        gt_bbox[:, 0] = norm_gt_bbox[:, 1] * float(width)
        gt_bbox[:, 1] = norm_gt_bbox[:, 0] * float(height)
        gt_bbox[:, 2] = norm_gt_bbox[:, 3] * float(width)
        gt_bbox[:, 3] = norm_gt_bbox[:, 2] * float(height)

        results['gt_bboxes'] = gt_bbox
        results['img'] = im
        results['img_shape'] = im.shape
        results['pad_shape'] = im.shape

        return results


@PIPELINES.register_module
class BBoxJitter(object):
    """
    bbox jitter
    Args:
        min (int, optional): min scale
        max (int, optional): max scale
        ## origin w scale
    """

    def __init__(self, min=0, max=2):
        self.min_scale = min
        self.max_scale = max

    def bbox_jitter(self, bboxes, img_shape):
        """Flip bboxes horizontally.
        Args:
            bboxes(ndarray): shape (..., 4*k)
            img_shape(tuple): (height, width)
        """
        assert bboxes.shape[-1] % 4 == 0
        if len(bboxes) == 0:
            return bboxes
        jitter_bboxes = []
        for box in bboxes:
            w = box[2] - box[0]
            h = box[3] - box[1]
            center_x = (box[0] + box[2]) / 2
            center_y = (box[1] + box[3]) / 2
            scale = np.random.uniform(self.min_scale, self.max_scale)
            w = w * scale / 2.
            h = h * scale / 2.
            xmin = center_x - w
            ymin = center_y - h
            xmax = center_x + w
            ymax = center_y + h
            box2 = np.array([xmin, ymin, xmax, ymax], dtype=np.float32)
            jitter_bboxes.append(box2)
        jitter_bboxes = np.array(jitter_bboxes, dtype=np.float32)
        jitter_bboxes[:, 0::2] = np.clip(jitter_bboxes[:, 0::2], 0, img_shape[1] - 1)
        jitter_bboxes[:, 1::2] = np.clip(jitter_bboxes[:, 1::2], 0, img_shape[0] - 1)
        return jitter_bboxes

    def __call__(self, results):
        for key in results.get('bbox_fields', []):
            results[key] = self.bbox_jitter(results[key],
                                          results['img_shape'])
        return results

    def __repr__(self):
        return self.__class__.__name__ + '(bbox_jitter={}-{})'.format(
            self.min_scale, self.max_scale)


@PIPELINES.register_module
class MotionBlur(object):
    """Motion blurring images
    Args:
        p (float): the motion blurring probability.

    """

    def __init__(self, p=None):
        self.p = p
        if p is not None:
            assert p >= 0 and p <= 1

    def motion_blur(self, img, img_shape):
        """Motion Blurring image.
        Args:
            img_shape(tuple): (height, width)
        """
        if img_shape[1] <= 750:
            kernel_size = int(random.sample(range(10, 20), 1)[0])
        elif img_shape[1] < 2000 and img_shape[1] > 750:
            kernel_size = int(random.sample(range(20, 60), 1)[0])
        elif img_shape[1] >= 2000:
            kernel_size = int(random.sample(range(100, 140), 1)[0])
        kernel_motion_blur = np.zeros((kernel_size, kernel_size))
        kernel_motion_blur[int((kernel_size - 1) / 2), :] = np.ones(kernel_size)
        kernel_motion_blur = kernel_motion_blur / kernel_size
        img = cv2.filter2D(img, -1, kernel_motion_blur)
        return img

    def __call__(self, results):
        if 'motion_blur' not in results:
            motion_blur = True if np.random.rand() < self.p else False
            results['motion_blur'] = motion_blur
        if results['motion_blur']:
            # motion blurring image
            results['img'] = self.motion_blur(results['img'], results['img_shape'])
        return results

    def __repr__(self):
        return self.__class__.__name__ + '(p={})'.format(
            self.p)


@PIPELINES.register_module
class Mixup(object):
    """Mixup images & bbox
    Args:
        prob (float): the probability of carrying out mixup process.
        lambd (float): the parameter for mixup.
        mixup (bool): mixup switch.
        json_path (string): the path to dataset json file.
    """

    def __init__(self, prob=0.5, lambd=0.5, mixup=False,
                 json_path='data/annotations/train.json',
                 img_path='data/images/'):
        self.lambd = lambd
        self.prob = prob
        self.mixup = mixup
        self.json_path = json_path
        self.img_path = img_path
        import json
        with open(json_path, 'r') as json_file:
            all_labels = json.load(json_file)
        self.all_labels = all_labels

    def get_img2(self):
        # random get image2 for mixup
        idx2 = np.random.choice(np.arange(len(self.all_labels['images'])))
        img2_fn = self.all_labels['images'][idx2]['file_name']
        img2_id = self.all_labels['images'][idx2]['id']
        img2_path = self.img_path + img2_fn
        img2 = cv2.imread(img2_path)

        # get image2 label
        labels2 = []
        boxes2 = []
        for annt in self.all_labels['annotations']:
            if annt['image_id'] == img2_id:
                labels2.append(np.int64(annt['category_id']))
                boxes2.append([np.float32(annt['bbox'][0]),
                               np.float32(annt['bbox'][1]),
                               np.float32(annt['bbox'][0] + annt['bbox'][2] - 1),
                               np.float32(annt['bbox'][1] + annt['bbox'][3] - 1)])
        return img2, labels2, boxes2

    def __call__(self, results):
        if self.mixup == True:
            if random.uniform(0, 1) > self.prob:
                img1 = results['img']
                labels1 = results['gt_labels']

                img2, labels2, boxes2 = self.get_img2()
                # if labels2 != []:
                #     break

                height = max(img1.shape[0], img2.shape[0])
                width = max(img1.shape[1], img2.shape[1])

                if labels2 == []:
                    self.lambd = 0.9  # float(round(random.uniform(0.5,0.9),1))
                    # mix image
                    mixup_image = np.zeros([height, width, 3], dtype='float32')
                    mixup_image[:img1.shape[0], :img1.shape[1], :] = img1.astype('float32') * self.lambd
                    mixup_image[:img2.shape[0], :img2.shape[1], :] += img2.astype('float32') * (1. - self.lambd)
                    mixup_image = mixup_image.astype('uint8')
                else:
                    # mix image
                    mixup_image = np.zeros([height, width, 3], dtype='float32')
                    mixup_image[:img1.shape[0], :img1.shape[1], :] = img1.astype('float32') * self.lambd
                    mixup_image[:img2.shape[0], :img2.shape[1], :] += img2.astype('float32') * (1. - self.lambd)
                    mixup_image = mixup_image.astype('uint8')

                    # mix labels
                    results['gt_labels'] = np.hstack((labels1, np.array(labels2)))
                    results['gt_bboxes'] = np.vstack((list(results['gt_bboxes']), boxes2))

                results['img'] = mixup_image

                # if the image2 has not bboxes, the 'gt_labels' and 'gt_bboxes' need to be doubled
                # so at the end the half of loss weight can be added as 1 instead of 0.5
                # if boxes2 == []:
                #     results['gt_labels'] = np.hstack((labels1, labels1))
                #     results['gt_bboxes'] = np.vstack((list(results['gt_bboxes']), list(results['gt_bboxes'])))
                # else:

                return results
            else:
                return results

    def __repr__(self):
        return self.__class__.__name__ + '(prob={}, lambd={}, mixup={}, json_path={}, img_path={})'.format(self.prob,
                                                                                                           self.lambd,
                                                                                                           self.mixup,
                                                                                                           self.json_path,
                                                                                                           self.img_path)


class GaussianBlurConv():
    '''
    高斯滤波
    依据图像金字塔和高斯可分离滤波器思路加速
    '''

    def FilterGaussian(self, img, sigma):
        '''
        高斯分离卷积，按照x轴y轴拆分运算，再合并，加速运算
        '''
        # reject unreasonable demands
        if sigma > 300:
            sigma = 300
        # 获取滤波器尺寸且强制为奇数
        kernel_size = round(sigma * 3 * 2 + 1) | 1  # 当图像类型为CV_8U的时候能量集中区域为3 * sigma,
        # 创建内核
        kernel = cv2.getGaussianKernel(ksize=kernel_size, sigma=sigma, ktype=cv2.CV_32F)
        # 初始化图像
        temp = np.zeros_like(img)
        # x轴滤波
        for j in range(temp.shape[0]):
            for i in range(temp.shape[1]):
                # 内层循环展开
                v1 = v2 = v3 = 0
                for k in range(kernel_size):
                    source = math.floor(
                        i + kernel_size / 2 - k)  # 把第i个坐标和kernel的中心对齐 -k是从右往左遍历kernel对应的图像，得到与kernel的第k个元素相乘的图像坐标
                    if source < 0:
                        source = source * -1  # 如果图像超出左边缘，就反向，对称填充
                    if source > img.shape[1]:
                        source = math.floor(2 * (img.shape[1] - 1) - source)  # 图像如果超出右边缘，就用左边从头数着补
                    v1 += kernel[k] * img[j, source, 0]
                    if temp.shape[2] == 1: continue
                    v2 += kernel[k] * img[j, source, 1]
                    v3 += kernel[k] * img[j, source, 2]
                temp[j, i, 0] = v1
                if temp.shape[2] == 1: continue
                temp[j, i, 1] = v2
                temp[j, i, 2] = v3
        # 分离滤波，先在原图用x轴的滤波器滤波，得到temp图，再用y轴滤波在temp图上滤波，结果一致
        # y轴滤波
        for i in range(img.shape[1]):  # height
            for j in range(img.shape[0]):
                v1 = v2 = v3 = 0
                for k in range(kernel_size):
                    source = math.floor(j + kernel_size / 2 - k)
                    if source < 0:
                        source = source * -1
                    if source > temp.shape[0]:
                        source = math.floor(2 * (img.shape[0] - 1) - source)  # 上下对称
                    v1 += kernel[k] * temp[source, i, 0]
                    if temp.shape[2] == 1: continue
                    v2 += kernel[k] * temp[source, i, 1]
                    v3 += kernel[k] * temp[source, i, 2]
                img[j, i, 0] = v1
                if img.shape[2] == 1: continue
                img[j, i, 1] = v2
                img[j, i, 2] = v3
        return img

    def FastFilter(self, img, sigma):
        '''
        快速滤波，按照图像金字塔，逐级降低图像分辨率，对应降低高斯核的sigma，
        当sigma转换成高斯核size小于10，再进行滤波，后逐级resize
        递归思路
        '''
        # reject unreasonable demands
        if sigma > 300:
            sigma = 300
        # 获取滤波尺寸，且强制为奇数
        kernel_size = round(sigma * 3 * 2 + 1) | 1  # 当图像类型为CV_8U的时候能量集中区域为3 * sigma,
        # 如果s*sigma小于一个像素，则直接退出
        if kernel_size < 3:
            return
        # 处理方式(1) 滤波  (2) 高斯光滑处理  (3) 递归处理滤波器大小
        if kernel_size < 10:
            # img = self.FilterGaussian(img, sigma)
            img = cv2.GaussianBlur(img, (kernel_size, kernel_size), 0)  # 官方函数
            return img
        else:
            # 若降采样到最小，直接退出
            if img.shape[1] < 2 or img.shape[0] < 2:
                return img
            sub_img = np.zeros_like(img)  # 初始化降采样图像
            sub_img = cv2.pyrDown(img, sub_img)  # 使用gaussian滤波对输入图像向下采样，缩放二分之一，仅支持CV_GAUSSIAN_5x5
            sub_img = self.FastFilter(sub_img, sigma / 2.0)
            img = cv2.resize(sub_img, (img.shape[1], img.shape[0]))  # resize到原图大小
            return img

    def __call__(self, x, sigma):
        x = self.FastFilter(x, sigma)
        return x


@PIPELINES.register_module
class Retinex(object):
    """
    SSR: baseline
    MSR: keep the high fidelity and the dynamic range as well as compressing img
    MSRCR_GIMP:
      Adapt the dynamics of the colors according to the statistics of the first and second order.
      The use of the variance makes it possible to control the degree of saturation of the colors.
    """

    def __init__(self, model='MSR', sigma=[30, 150, 300], restore_factor=2.0, color_gain=10.0, gain=270.0,
                 offset=128.0):
        self.model_list = ['SSR', 'MSR']
        if model in self.model_list:
            self.model = model
        else:
            raise ValueError
        self.sigma = sigma  # 高斯核的方差
        # 颜色恢复
        self.restore_factor = restore_factor  # 控制颜色修复的非线性
        self.color_gain = color_gain  # 控制颜色修复增益
        # 图像恢复
        self.gain = gain  # 图像像素值改变范围的增益
        self.offset = offset  # 图像像素值改变范围的偏移量
        self.gaussian_conv = GaussianBlurConv()  # 实例化高斯算子

    def _SSR(self, img, sigma):
        filter_img = self.gaussian_conv(img, sigma)  # [h,w,c]
        retinex = np.log10(img) - np.log10(filter_img)
        return retinex

    def _MSR(self, img, simga):
        retinex = np.zeros_like(img)
        for sig in simga:
            retinex += self._SSR(img, sig)
        retinex = retinex / float(len(self.sigma))
        return retinex

    def _colorRestoration(self, img, retinex):
        img_sum = np.sum(img, axis=2, keepdims=True)  # 在通道层面求和
        # 颜色恢复
        # 权重矩阵归一化 并求对数，得到颜色增益
        color_restoration = np.log10((img * self.restore_factor / img_sum) * 1.0 + 1.0)
        # 将Retinex做差后的图像，按照权重和颜色增益重新组合
        img_merge = retinex * color_restoration * self.color_gain
        # 恢复图像
        img_restore = img_merge * self.gain + self.offset
        return img_restore

    def _simplestColorBalance(self, img, low_clip, high_clip):
        total = img.shape[0] * img.shape[1]
        for i in range(img.shape[2]):
            unique, counts = np.unique(img[:, :, i], return_counts=True)  # 返回新列表元素在旧列表中的位置，并以列表形式储存在s中
            current = 0
            for u, c in zip(unique, counts):
                if float(current) / total < low_clip:
                    low_val = u
                if float(current) / total < high_clip:
                    high_val = u
                current += c
            img[:, :, i] = np.maximum(np.minimum(img[:, :, i], high_val), low_val)
        return img

    def _MSRCR_GIMP(self, results):

        self.img = results['img']
        self.img = np.float32(self.img) + 1.0
        if self.model == 'SSR':
            self.retinex = self._SSR(self.img, self.sigma)
        elif self.model == 'MSR':
            self.retinex = self._MSR(self.img, self.sigma)
        # 颜色恢复 图像恢复
        self.img_restore = self._colorRestoration(self.img, self.retinex)
        results['img'] = self.img_restore

    def __call__(self, results):
        self._MSRCR_GIMP(results)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += '{},sigma={},dynamic={}'.format(self.model, self.sigma)
        return repr_str

import copy
@PIPELINES.register_module()
class Mosaic(object):
    """Mosaic augmentation.
    Given 4 images, Mosaic augmentation randomly crop a patch on each image
    and combine them into one output image. The output image is composed of
    the parts from each sub-image.
                        output image
                                cut_x
               +-----------------------------+
               |     image 0      | image 1  |
               |                  |          |
        cut_y  |------------------+----------|
               |     image 2      | image 3  |
               |                  |          |
               |                  |          |
               |                  |          |
               |                  |          |
               |                  |          |
               +-----------------------------+
    Args:
        size (tuple[int]): output image size in (h,w).
        min_offset (float | tuple[float]): Volume of the offset
            of the cropping window. If float, both height and width are
        dataset (torch.nn.Dataset): Dataset with augmentation pipeline.
    """

    def __init__(self, size=(640, 640), min_offset=0.2, dataset=None):

        assert isinstance(size, tuple)
        assert isinstance(size[0], int) and isinstance(size[1], int)
        if size[0] <= 0 or size[1] <= 0:
            raise ValueError('image size must > 0 in train mode')

        if isinstance(min_offset, float):
            assert 0 <= min_offset <= 1
            self.min_offset = (min_offset, min_offset)
        elif isinstance(min_offset, tuple):
            assert isinstance(min_offset[0], float) \
                   and isinstance(min_offset[1], float)
            assert 0 <= min_offset[0] <= 1 and \
                   0 <= min_offset[1] <= 1
            self.min_offset = min_offset
        else:
            raise TypeError('Unsupported type for min_offset, '
                            'should be either float or tuple[float]')

        self.size = size
        self.dataset = dataset
        self.cropper = RandomCrop(crop_size=size)
        self.num_sample = len(dataset)

    def __call__(self, results):
        """Call the function to mix 4 images.
        Args:
            results (dict): Result dict from loading pipeline.
        Returns:
            dict: Result dict with images and bounding boxes cropped.
        """
        # Generate the Mosaic coordinate
        cut_y = random.randint(
            int(self.size[0] * self.min_offset[0]),
            int(self.size[0] * (1 - self.min_offset[0])))
        cut_x = random.randint(
            int(self.size[1] * self.min_offset[1]),
            int(self.size[1] * (1 - self.min_offset[1])))

        cut_position = (cut_y, cut_x)
        tmp_result = copy.deepcopy(results)
        # create the image buffer and mask buffer
        tmp_result['img'] = np.zeros(
            (self.size[0], self.size[1], *tmp_result['img'].shape[2:]),
            dtype=tmp_result['img'].dtype)

        for key in tmp_result.get('seg_fields', []):
            tmp_result[key] = np.zeros(
                (self.size[0], self.size[1], *tmp_result[key].shape[2:]),
                dtype=tmp_result[key].dtype)
        tmp_result['img_shape'] = self.size

        out_bboxes = []
        out_labels = []
        out_ignores = []

        for loc in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
            if loc == 'top_left':
                # use the current image
                results_i = copy.deepcopy(results)
            else:
                # randomly sample a new image from the dataset
                index = random.randint(self.num_sample)
                results_i = copy.deepcopy(self.dataset.__getitem__(index))

            # compute the crop parameters
            crop_size, img_slices, paste_position = self._mosiac_combine(
                loc, cut_position)

            self.cropper.crop_size = crop_size
            results_i = self.cropper(results_i)

            tmp_result['img'][img_slices] = results_i['img'].copy()
            for key in tmp_result.get('seg_fields', []):
                tmp_result[key][img_slices] = results_i[key].copy()

            results_i = self._adjust_coordinate(results_i, paste_position)

            out_bboxes.append(results_i['gt_bboxes'])
            out_labels.append(results_i['gt_labels'])
            out_ignores.append(results_i['gt_bboxes_ignore'])

        out_bboxes = np.concatenate(out_bboxes, axis=0)
        out_labels = np.concatenate(out_labels, axis=0)
        out_ignores = np.concatenate(out_ignores, axis=0)

        tmp_result['gt_bboxes'] = out_bboxes
        tmp_result['gt_labels'] = out_labels
        tmp_result['gt_bboxes_ignore'] = out_ignores

        return tmp_result

    def _mosiac_combine(self, loc, cut_position):
        """Crop the subimage, change the label and mix the image.
        Args:
            loc (str): Index for the subimage, loc in ('top_left',
                'top_right', 'bottom_left', 'bottom_right').
            results (dict): Result dict from loading pipeline.
            img (numpy array): buffer for mosiac image, (H x W x 3).
            cut_position (tuple[int]): mixing center for 4 images, (y, x).
        Returns:
            bboxes: Result dict with images and bounding boxes cropped.
        """
        if loc == 'top_left':
            # Image 0: top left
            crop_size = cut_position
            img_slices = (slice(0, cut_position[0]), slice(0, cut_position[1]))
            paste_position = (0, 0)
        elif loc == 'top_right':
            # Image 1: top right
            crop_size = (cut_position[0], self.size[1] - cut_position[1])
            img_slices = (slice(0, cut_position[0]),
                          slice(cut_position[1], self.size[1]))
            paste_position = (0, cut_position[1])
        elif loc == 'bottom_left':
            # Image 2: bottom left
            crop_size = (self.size[0] - cut_position[0], cut_position[1])
            img_slices = (slice(cut_position[0],
                                self.size[0]), slice(0, cut_position[1]))
            paste_position = (cut_position[0], 0)
        elif loc == 'bottom_right':
            # Image 3: bottom right
            crop_size = (self.size[0] - cut_position[0],
                         self.size[1] - cut_position[1])
            img_slices = (slice(cut_position[0], self.size[0]),
                          slice(cut_position[1], self.size[1]))
            paste_position = cut_position

        return crop_size, img_slices, paste_position

    def _adjust_coordinate(self, results, paste_position):
        """Convert subimage coordinate to mosaic image coordinate.
         Args:
            results (dict): Result dict from :obj:`dataset`.
            paste_position (tuple[int]): paste up-left corner
                coordinate (y, x) in moaiac image.
        Returns:
            results (dict): Result dict with corrected bbox
                and mask coordinate.
        """

        for key in results.get('bbox_fields', []):
            box = results[key]
            box[:, 0::2] += paste_position[1]
            box[:, 1::3] += paste_position[0]
            results[key] = box

        return results
