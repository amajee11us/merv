# ruff: noqa

import math
import warnings

import decord
import numpy as np
import torch
from decord import VideoReader, cpu
from torchvision.transforms import Compose, Lambda
from transformers import ProcessorMixin

with warnings.catch_warnings(record=True):
    warnings.simplefilter("always")
    from torchvision.transforms._transforms_video import (
        CenterCropVideo,
        NormalizeVideo,
        RandomHorizontalFlipVideo,
    )

decord.bridge.set_bridge("torch")

OPENAI_DATASET_MEAN = (0.48145466, 0.4578275, 0.40821073)
OPENAI_DATASET_STD = (0.26862954, 0.26130258, 0.27577711)


def make_list_of_images(x):
    if not isinstance(x, list):
        return [x]
    return x


class ShortSideScale(torch.nn.Module):
    """
    ``nn.Module`` wrapper for ``pytorchvideo.transforms.functional.short_side_scale``.
    Get around downloading an entire package just for this one method.
    """

    def __init__(self, size: int, interpolation: str = "bilinear"):
        super().__init__()
        self._size = size
        self._interpolation = interpolation

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x (torch.Tensor): video tensor with shape (C, T, H, W).
        """
        assert len(x.shape) == 4
        assert x.dtype == torch.float32
        size = self._size
        interpolation = self._interpolation
        c, t, h, w = x.shape
        if w < h:
            new_h = int(math.floor((float(h) / w) * size))
            new_w = size
        else:
            new_h = size
            new_w = int(math.floor((float(w) / h) * size))
        return torch.nn.functional.interpolate(x, size=(new_h, new_w), mode=interpolation, align_corners=False)


def get_video_transform(video_decode_backend="decord"):
    if video_decode_backend == "decord":
        transform = Compose(
            [
                # UniformTemporalSubsample(num_frames),
                Lambda(lambda x: x / 255.0),
                NormalizeVideo(mean=OPENAI_DATASET_MEAN, std=OPENAI_DATASET_STD),
                ShortSideScale(size=224),
                CenterCropVideo(224),
                RandomHorizontalFlipVideo(p=0.5),
            ]
        )
    else:
        raise NameError(
            f"video_decode_backend should specify in (pytorchvideo, decord, opencv) but got {video_decode_backend}"
        )
    return transform


def load_and_transform_video(
    video_path,
    transform,
    video_decode_backend="decord",
    clip_start_sec=0.0,
    clip_end_sec=None,
    num_frames=8,
):
    if video_decode_backend == "decord":
        decord.bridge.set_bridge("torch")
        decord_vr = VideoReader(video_path, ctx=cpu(0))
        duration = len(decord_vr)
        frame_id_list = np.linspace(0, duration - 1, num_frames, dtype=int)
        video_data = decord_vr.get_batch(frame_id_list)
        video_data = video_data.permute(3, 0, 1, 2)  # (T, H, W, C) -> (C, T, H, W)
        video_outputs = transform(video_data)
    else:
        raise NameError("video_decode_backend should specify in (pytorchvideo, decord, opencv)")
    return video_outputs


class LanguageBindVideoProcessor(ProcessorMixin):
    attributes = []
    tokenizer_class = "LanguageBindVideoTokenizer"

    def __init__(self, config, tokenizer=None, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.transform = get_video_transform(config.vision_config.video_decode_backend)
        self.image_processor = load_and_transform_video
        self.tokenizer = tokenizer

    def __call__(self, images=None, text=None, context_length=77, return_tensors=None, **kwargs):
        if text is None and images is None:
            raise ValueError("You have to specify either text or images. Both cannot be none.")

        if text is not None:
            encoding = self.tokenizer(
                text,
                max_length=context_length,
                padding="max_length",
                truncation=True,
                return_tensors=return_tensors,
                **kwargs,
            )

        if images is not None:
            images = make_list_of_images(images)
            image_features = [
                self.image_processor(
                    image,
                    self.transform,
                    video_decode_backend=self.config.vision_config.video_decode_backend,
                    num_frames=self.config.vision_config.num_frames,
                )
                for image in images
            ]
            image_features = torch.stack(image_features)

        if text is not None and images is not None:
            encoding["pixel_values"] = image_features
            return encoding
        elif text is not None:
            return encoding
        else:
            return {"pixel_values": image_features}

    def preprocess(self, images, return_tensors):
        return self.__call__(images=images, return_tensors=return_tensors)

    def batch_decode(self, skip_special_tokens=True, *args, **kwargs):
        """
        This method forwards all its arguments to CLIPTokenizerFast's [`~PreTrainedTokenizer.batch_decode`]. Please
        refer to the docstring of this method for more information.
        """
        return self.tokenizer.batch_decode(*args, skip_special_tokens=skip_special_tokens, **kwargs)

    def decode(self, skip_special_tokens=True, *args, **kwargs):
        """
        This method forwards all its arguments to CLIPTokenizerFast's [`~PreTrainedTokenizer.decode`]. Please refer to
        the docstring of this method for more information.
        """
        return self.tokenizer.decode(*args, skip_special_tokens=skip_special_tokens, **kwargs)
