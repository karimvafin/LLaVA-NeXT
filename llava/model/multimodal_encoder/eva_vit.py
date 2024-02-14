# Based on EVA, BEIT, timm and DeiT code bases
# https://github.com/baaivision/EVA
# https://github.com/rwightman/pytorch-image-models/tree/master/timm
# https://github.com/microsoft/unilm/tree/master/beit
# https://github.com/facebookresearch/deit/
# https://github.com/facebookresearch/dino
# --------------------------------------------------------'
import math
from functools import partial

from transformers import CLIPImageProcessor

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as checkpoint
from timm.models.layers import drop_path, to_2tuple, trunc_normal_
from .eva_clip import create_model_and_transforms, get_tokenizer
import torch
import torchvision
import time

from llava.utils import rank0_print

class EvaViTWrapper(nn.Module):
    def __init__(self, vision_tower, args, delay_load=False):
        super().__init__()

        self.is_loaded = False

        self.vision_tower_name = vision_tower
        self.pretrained = args.vision_tower_pretrained
        self.args = args

        self.select_layer = args.mm_vision_select_layer
        if self.select_layer < -1:
            self.select_layer += 1
        self.select_feature = getattr(args, "mm_vision_select_feature", "patch")

        if not delay_load:
            self.load_model()

    def load_model(self):
        rank0_print(f"Loading EVA ViT: {self.vision_tower_name}")
        rank0_print(f"Pretrained: {self.pretrained}")
        time_start = time.time()
        model, _, image_processor = create_model_and_transforms(self.vision_tower_name, self.pretrained, force_custom_clip=True, precision="bf16", device="cuda")
        time_end = time.time()
        rank0_print(f"Loaded EVA ViT: {self.vision_tower_name} in {time_end - time_start:.2f}s")
        self.device = next(model.parameters()).device
        self.dtype = next(model.parameters()).dtype
        self.vision_tower = model.visual
        resize_transform = [t for t in image_processor.transforms if isinstance(t, torchvision.transforms.Resize)][0]
        normalize_transform = [t for t in image_processor.transforms if isinstance(t, torchvision.transforms.Normalize)][0]
        self.resize_transform_size = resize_transform.size
        self.image_processor = CLIPImageProcessor.from_pretrained(
            "openai/clip-vit-large-patch14",
            crop_size=resize_transform.size,
            size={"shortest_edge": resize_transform.size},
            image_mean=list(normalize_transform.mean),
            image_std=list(normalize_transform.std),
        )
        for p in self.vision_tower.parameters():
            p.requires_grad = False
        self.vision_tower.eval()
        self.is_loaded = True

    def feature_select(self, image_features):
        select_feature_type = self.select_feature

        if self.select_feature in ["slicefour_patch", "slicefour_cls_patch"]:
            select_every_k_layer = len(image_features) // 4
            image_features = torch.cat([image_features[i] for i in range(select_every_k_layer + self.select_layer, len(image_features), select_every_k_layer)], dim=-1)
            select_feature_type = select_feature_type.replace("slicefour_", "")
        elif self.select_feature in ["slice_m25811_f6_patch", "slice_m25811_f6_cls_patch"]:
            select_layers = [-1, -4, -7, -10, 6]
            image_features = torch.cat([image_features[i] for i in select_layers], dim=-1)
            select_feature_type = select_feature_type.replace("slice_m25811_f6_", "")
        else:
            image_features = image_features[self.select_layer]

        if select_feature_type == "patch":
            image_features = image_features[:, 1:]
        elif select_feature_type == "cls_patch":
            image_features = image_features
        else:
            raise ValueError(f"Unexpected select feature: {select_feature_type}")
        return image_features

    def train(self, mode=True):
        self.training = mode

        if self.is_loaded:
            self.vision_tower.eval()

    @torch.no_grad()
    def forward(self, images):
        if type(images) is list:
            image_features = []
            for image in images:
                image_forward_out = self.vision_tower(image.to(self.dtype)).unsqueeze(0)
                image_feature = self.feature_select(image_forward_out).to(self.dtype)
                image_features.append(image_feature)
        else:
            image_forward_outs = self.vision_tower(images.to(self.dtype)).unsqueeze(0)
            image_features = self.feature_select(image_forward_outs).to(self.dtype)

        return image_features

    @property
    def dummy_feature(self):
        return torch.zeros(1, 1408, device=self.device, dtype=self.dtype)

    @property
    def num_patches(self):
        _num_patches = 256
        if "cls_patch" in self.select_feature:
            _num_patches += 1
        return _num_patches

    @property
    def hidden_size(self):
        _hidden_size = 1408
        if "slicefour" in self.select_feature:
            _hidden_size *= 4
        if "slice_m25811_f6" in self.select_feature:
            _hidden_size *= 5
        return _hidden_size

    @property
    def config(self):
        return type(
            "LLaVAConfigWrapper",
            (),
            {
                "image_size": 224,
                "patch_size": 14,
            },
        )()
