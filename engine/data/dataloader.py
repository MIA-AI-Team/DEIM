"""
DEIM: DETR with Improved Matching for Fast Convergence
Copyright (c) 2024 The DEIM Authors. All Rights Reserved.
---------------------------------------------------------------------------------
Modified from D-FINE (https://github.com/Peterande/D-FINE)
Copyright (c) 2024 D-FINE authors. All Rights Reserved.
"""

import torch
import torch.utils.data as data
import torch.nn.functional as F
from torch.utils.data import default_collate

import torchvision
import torchvision.transforms.v2 as VT
from torchvision.transforms.v2 import functional as VF, InterpolationMode

import random
from functools import partial

from ..core import register
torchvision.disable_beta_transforms_warning()
from copy import deepcopy
from PIL import Image, ImageDraw
import os
import gc


__all__ = [
    'DataLoader',
    'BaseCollateFunction',
    'BatchImageCollateFunction',
    'batch_image_collate_fn'
]


@register()
class DataLoader(data.DataLoader):
    __inject__ = ['dataset', 'collate_fn']

    def __repr__(self) -> str:
        format_string = self.__class__.__name__ + "("
        for n in ['dataset', 'batch_size', 'num_workers', 'drop_last', 'collate_fn']:
            format_string += "\n"
            format_string += "    {0}: {1}".format(n, getattr(self, n))
        format_string += "\n)"
        return format_string

    def set_epoch(self, epoch):
        self._epoch = epoch
        self.dataset.set_epoch(epoch)
        self.collate_fn.set_epoch(epoch)

    @property
    def epoch(self):
        return self._epoch if hasattr(self, '_epoch') else -1

    @property
    def shuffle(self):
        return self._shuffle

    @shuffle.setter
    def shuffle(self, shuffle):
        assert isinstance(shuffle, bool), 'shuffle must be a boolean'
        self._shuffle = shuffle


@register()
def batch_image_collate_fn(items):
    """only batch image
    """
    return torch.cat([x[0][None] for x in items], dim=0), [x[1] for x in items]


class BaseCollateFunction(object):
    def set_epoch(self, epoch):
        self._epoch = epoch
        # Print notification when epoch is about to transition to a new phase
        if hasattr(self, 'mixup_epochs') and len(self.mixup_epochs) >= 2:
            if epoch == self.mixup_epochs[0]:
                print(f"Epoch {epoch}: Activating mixup augmentation")
            elif epoch == self.mixup_epochs[-1]:
                print(f"Epoch {epoch}: Deactivating mixup augmentation")

    @property
    def epoch(self):
        return self._epoch if hasattr(self, '_epoch') else -1

    def __call__(self, items):
        raise NotImplementedError('')


def generate_scales(base_size, base_size_repeat):
    scale_repeat = (base_size - int(base_size * 0.75 / 32) * 32) // 32
    scales = [int(base_size * 0.75 / 32) * 32 + i * 32 for i in range(scale_repeat)]
    scales += [base_size] * base_size_repeat
    scales += [int(base_size * 1.25 / 32) * 32 - i * 32 for i in range(scale_repeat)]
    return scales


@register() 
class BatchImageCollateFunction(BaseCollateFunction):
    def __init__(
        self, 
        stop_epoch=None, 
        ema_restart_decay=0.9999,
        base_size=640,
        base_size_repeat=None,
        mixup_prob=0.0,
        mixup_epochs=[0, 0],
        data_vis=False,
        vis_save='./vis_dataset/',
        gradual_mixup=True  # New parameter to enable gradual mixup introduction
    ) -> None:
        super().__init__()
        self.base_size = base_size
        self.scales = generate_scales(base_size, base_size_repeat) if base_size_repeat is not None else None
        self.stop_epoch = stop_epoch if stop_epoch is not None else 100000000
        self.ema_restart_decay = ema_restart_decay
        # FIXME Mixup
        self.mixup_prob, self.mixup_epochs = mixup_prob, mixup_epochs
        self.gradual_mixup = gradual_mixup
        self.original_mixup_prob = mixup_prob  # Store original probability
        
        if self.mixup_prob > 0:
            self.data_vis, self.vis_save = data_vis, vis_save
            os.makedirs(self.vis_save, exist_ok=True) if self.data_vis else None
            print("     ### Using MixUp with Prob@{} in {} epochs ### ".format(self.mixup_prob, self.mixup_epochs))
            # For gradual introduction of mixup
            if self.gradual_mixup and len(self.mixup_epochs) >= 2:
                print(f"     ### Using gradual mixup introduction starting at epoch {self.mixup_epochs[0]} ###")
        
        if stop_epoch is not None:
            print("     ### Multi-scale Training until {} epochs ### ".format(self.stop_epoch))
            print("     ### Multi-scales@ {} ###        ".format(self.scales))
        
        self.print_info_flag = True
        self.warm_up_epochs = 3  # Number of epochs for gradual warm-up

    def set_epoch(self, epoch):
        super().set_epoch(epoch)
        
        # Implement gradual mixup introduction to avoid memory spikes
        if self.gradual_mixup and self.mixup_prob > 0:
            if len(self.mixup_epochs) >= 2 and self.mixup_epochs[0] <= epoch < self.mixup_epochs[-1]:
                # Calculate how many warm-up epochs we have
                epochs_since_start = epoch - self.mixup_epochs[0]
                
                if epochs_since_start < self.warm_up_epochs:
                    # Gradually increase mixup probability over the first few epochs
                    adjusted_prob = self.original_mixup_prob * ((epochs_since_start + 1) / self.warm_up_epochs)
                    if self.mixup_prob != adjusted_prob:
                        self.mixup_prob = adjusted_prob
                        print(f"Epoch {epoch}: Adjusting mixup probability to {self.mixup_prob:.4f}")
                elif self.mixup_prob != self.original_mixup_prob:
                    # Reset to original probability after warm-up
                    self.mixup_prob = self.original_mixup_prob
                    print(f"Epoch {epoch}: Mixup at full probability {self.mixup_prob}")

    def apply_mixup(self, images, targets):
        """
        Applies Mixup augmentation to the batch if conditions are met.

        Args:
            images (torch.Tensor): Batch of images.
            targets (list[dict]): List of target dictionaries corresponding to images.

        Returns:
            tuple: Updated images and targets
        """
        # Log when Mixup is permanently disabled
        if self.epoch == self.mixup_epochs[-1] and self.print_info_flag:
            print(f"     ### Attention --- Mixup is closed after epoch@ {self.epoch} ###")
            self.print_info_flag = False

        # Apply Mixup if within specified epoch range and probability threshold
        if random.random() < self.mixup_prob and self.mixup_epochs[0] <= self.epoch < self.mixup_epochs[-1]:
            # Generate mixup ratio
            beta = round(random.uniform(0.45, 0.55), 6)

            # Memory-optimized mixup - avoid unnecessary copies
            # Create rolled version of images (more memory efficient than copying)
            rolled_images = images.roll(shifts=1, dims=0)
            
            # Perform mixup in place
            images.mul_(beta).add_(rolled_images.mul_(1.0 - beta))
            
            # Force garbage collection to free up memory
            rolled_images = None
            gc.collect()
            
            # Shifted targets reference
            shifted_targets = targets[-1:] + targets[:-1]
            
            # Memory-optimized target merging
            for i in range(len(targets)):
                # Use inplace operations where possible
                targets[i]['boxes'] = torch.cat([targets[i]['boxes'], shifted_targets[i]['boxes']], dim=0)
                targets[i]['labels'] = torch.cat([targets[i]['labels'], shifted_targets[i]['labels']], dim=0)
                targets[i]['area'] = torch.cat([targets[i]['area'], shifted_targets[i]['area']], dim=0)

                # Add mixup ratio to targets
                targets[i]['mixup'] = torch.tensor(
                    [beta] * len(targets[i]['boxes']) + [1.0 - beta] * len(shifted_targets[i]['boxes']), 
                    dtype=torch.float32
                )

            shifted_targets = None  # Clear reference to allow garbage collection
            gc.collect()

            if self.data_vis:
                for i in range(min(2, len(targets))):  # Limit visualization to first 2 samples to save memory
                    image_tensor = images[i]
                    image_tensor_uint8 = (image_tensor * 255).type(torch.uint8)
                    image_numpy = image_tensor_uint8.numpy().transpose((1, 2, 0))
                    pilImage = Image.fromarray(image_numpy)
                    draw = ImageDraw.Draw(pilImage)
                    print('mix_vis:', i, 'boxes.len=', len(targets[i]['boxes']))
                    for box in targets[i]['boxes']:
                        draw.rectangle([int(box[0]*640 - (box[2]*640)/2), int(box[1]*640 - (box[3]*640)/2), 
                                        int(box[0]*640 + (box[2]*640)/2), int(box[1]*640 + (box[3]*640)/2)], outline=(255,255,0))
                    pilImage.save(self.vis_save + str(i) + "_"+ str(len(targets[i]['boxes'])) +'_out.jpg')
                    # Clean up to save memory
                    del image_tensor_uint8, image_numpy, pilImage, draw
                    gc.collect()

        return images, targets

    def __call__(self, items):
        try:
            # Check if we're approaching a memory-intensive epoch
            current_epoch = self.epoch
            next_is_transition = False
            if hasattr(self, 'mixup_epochs') and len(self.mixup_epochs) >= 2:
                next_is_transition = current_epoch + 1 == self.mixup_epochs[0]
            
            # If about to transition, clear memory proactively
            if next_is_transition:
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            
            # Collect images with minimal intermediate copies
            images = torch.cat([x[0][None] for x in items], dim=0)
            targets = [x[1] for x in items]

            # Apply mixup - optimized for memory
            images, targets = self.apply_mixup(images, targets)

            # Apply resizing if needed
            if self.scales is not None and self.epoch < self.stop_epoch:
                sz = random.choice(self.scales)
                images = F.interpolate(images, size=sz)
                if 'masks' in targets[0]:
                    for tg in targets:
                        tg['masks'] = F.interpolate(tg['masks'], size=sz, mode='nearest')
                    raise NotImplementedError('')

            return images, targets
        
        except RuntimeError as e:
            if "CUDA out of memory" in str(e):
                print(f"CUDA OOM in BatchImageCollateFunction during epoch {self.epoch}")
                print(f"Memory-intensive operations: mixup_active={self.mixup_epochs[0] <= self.epoch < self.mixup_epochs[-1] if len(self.mixup_epochs) >= 2 else False}")
                # Clear memory and try to recover
                gc.collect()
                torch.cuda.empty_cache()
            raise  # Re-raise the exception after logging
