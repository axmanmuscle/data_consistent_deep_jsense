#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
sys.path.insert(0, '.')

import torch, os, glob, copy
import numpy as np
from tqdm import tqdm
from dotmap import DotMap

from datagen import MCFullFastMRI, crop
from models import MoDLDoubleUnroll
from losses import SSIMLoss, MCLoss
from utils import ifft

from torch.optim import Adam
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torch.nn import functional as F
from matplotlib import pyplot as plt

import argparse

parser = argparse.ArgumentParser()

parser.add_argument('--train_data', default='/mnt/h/FastMRI/knee/singlecoil_train',
                    type=str, help='Training data directory')
parser.add_argument('--val_data', default='/mnt/h/FastMRI/knee/singlecoil_val',
                    type=str, help='Training data directory')

args = parser.parse_args()
print(args.train_data)
print(args.val_data)

exit()

plt.rcParams.update({'font.size': 18})
plt.ioff(); plt.close('all')

os.environ["CUDA_DEVICE_ORDER"]    = "PCI_BUS_ID";
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
# Fix seed
global_seed = 2000
torch.manual_seed(global_seed)
np.random.seed(global_seed)
# Enable cuDNN kernel selection
torch.backends.cudnn.benchmark = True

# Training files
core_dir    = '/mnt/h/FastMRI/knee/singlecoil_train'
# maps_dir    = '/media/marius/easystore/marius/multicoil_train_Wc0_Espirit_maps'
train_files = sorted(glob.glob(core_dir + '/*.h5'))
# train_maps  = sorted(glob.glob(maps_dir + '/*.h5'))
# Validation files
core_dir  = '/mnt/h/FastMRI/knee/singlecoil_val'
# maps_dir  = '/media/marius/easystore/marius/multicoil_val_Wc0_Espirit_maps'
val_files = sorted(glob.glob(core_dir + '/*.h5'))
# val_maps  = sorted(glob.glob(maps_dir + '/*.h5'))

# How much data are we using
# 'num_slices' around 'central_slice' from each scan
center_slice = 15 # Reasonable for fMRI
num_slices   = 5 # Around center

# Config
hparams         = DotMap()
hparams.mode    = 'DeepJSense'
hparams.logging = False

# Image-ResNet parameters
# !!! Map-ResNet uses exactly the same for now
hparams.img_channels = 64
hparams.img_blocks   = 4
hparams.img_sep      = False # Do we use separate networks at each unroll?
# Data
hparams.downsample = 4 # R
hparams.use_acs    = True
hparams.acs_lines  = 1 # Ignored if 'use_acs' = True
# Model
hparams.use_img_net        = True
hparams.use_map_net        = True
hparams.map_init           = 'estimated'
hparams.img_init           = 'estimated'
hparams.mps_kernel_shape   = [15, 15, 9] # Always 15 coils
hparams.l2lam_init         = 0.01
hparams.l2lam_train        = True
hparams.meta_unrolls_start = 1 # Starting value
hparams.meta_unrolls_end   = 6 # Ending value
hparams.meta_preload       = 1 # Warm start from unrolls
hparams.block1_max_iter    = 3
hparams.block2_max_iter    = 3
hparams.cg_eps             = 1e-6
hparams.verbose            = False
# Static training parameters
hparams.lr           = 2e-3 # Finetune if desired
hparams.step_size    = 10 # Number of epochs to decay with gamma
hparams.decay_gamma  = 0.5
hparams.grad_clip    = 1. # Clip gradients
hparams.start_epoch  = 0 # Warm start from a specific epoch
hparams.batch_size   = 1 # !!! Unsupported !!!
hparams.pix_lam      = 0.
hparams.ssim_lam     = 1.
hparams.coil_lam     = 0.

# Global directory
global_dir = 'models/MapSize%dx%d_lamTrain%d' % (
    hparams.mps_kernel_shape[-2], hparams.mps_kernel_shape[-1],
    hparams.l2lam_train)
if not os.path.exists(global_dir):
    os.makedirs(global_dir)

# Datasets
train_dataset = MCFullFastMRI(train_files, num_slices, center_slice,
                              downsample=hparams.downsample,
                              use_acs=hparams.use_acs, acs_lines=hparams.acs_lines,
                              mps_kernel_shape=hparams.mps_kernel_shape,
                              maps=None)
val_dataset   = MCFullFastMRI(val_files, num_slices, center_slice,
                              use_acs=hparams.use_acs, acs_lines=hparams.acs_lines,
                              downsample=hparams.downsample, scramble=True, 
                              mps_kernel_shape=hparams.mps_kernel_shape,
                              maps=None)
train_loader  = DataLoader(train_dataset, batch_size=hparams.batch_size, 
                           shuffle=True, num_workers=8, drop_last=True)
val_loader    = DataLoader(val_dataset, batch_size=hparams.batch_size,
                           shuffle=False)

# Get a sample-specific model
model = MoDLDoubleUnroll(hparams)
model = model.cuda()
# Switch to train
model.train()
# Count parameters
total_params = np.sum([np.prod(p.shape) for p
                       in model.parameters() if p.requires_grad])
print('Total parameters %d' % total_params)

# Criterions
ssim           = SSIMLoss().cuda()
multicoil_loss = MCLoss().cuda()
pixel_loss     = torch.nn.MSELoss(reduction='sum')

# For each number of unrolls
for num_unrolls in range(hparams.meta_unrolls_start, hparams.meta_unrolls_end+1):
    # Warm-up or not
    if num_unrolls < hparams.meta_unrolls_end:
        hparams.num_epochs = 1
    else:
        hparams.num_epochs = 30 # 20 is sufficient for five slices
    
    # Get optimizer and scheduler
    optimizer = Adam(model.parameters(), lr=hparams.lr)
    scheduler = StepLR(optimizer, hparams.step_size, 
                       gamma=hparams.decay_gamma)
    
    # If we're beyond the first step, preload weights and state
    if num_unrolls > hparams.meta_preload:
        target_dir = global_dir + '/\
N%d_n%d_ACSlines%d' % (
            num_unrolls-1, hparams.block1_max_iter,
            hparams.acs_lines)
        # Get and load previous weights
        contents = torch.load(target_dir + '/ckpt_epoch%d.pt' % 0)
        # contents = torch.load(target_dir + '/best_weights.pt')
        model.load_state_dict(contents['model_state_dict'])
        optimizer.load_state_dict(contents['optimizer_state_dict'])
    
    # Logs
    best_loss = np.inf
    ssim_log = []
    loss_log = []
    coil_log = []
    running_loss, running_ssim, running_coil = 0, -1., 0.
    local_dir = global_dir + '/\
N%d_n%d_ACSlines%d' % (
            num_unrolls, hparams.block1_max_iter,
            hparams.acs_lines)
    if not os.path.isdir(local_dir):
        os.makedirs(local_dir)
    
    # Preload from the same model hyperparameters
    if hparams.start_epoch > 0:
        contents = torch.load(local_dir + '/ckpt_epoch%d.pt' % (hparams.start_epoch-1))
        model.load_state_dict(contents['model_state_dict'])
        optimizer.load_state_dict(contents['optimizer_state_dict'])
        # Increment scheduler
        scheduler.last_epoch = hparams.start_epoch-1
    
    # For each epoch
    for epoch_idx in range(hparams.start_epoch, hparams.num_epochs):
        # For each batch
        for sample_idx, sample in tqdm(enumerate(train_loader)):
            # Move to CUDA
            for key in sample.keys():
                try:
                    sample[key] = sample[key].cuda()
                except:
                    pass
            
            # Get outputs
            est_img_kernel, est_map_kernel, est_ksp = \
                model(sample, num_unrolls)
            
            # Extra padding with zero lines - to restore resolution
            est_ksp_padded = F.pad(est_ksp, ( 
                    torch.sum(sample['dead_lines'] < est_ksp.shape[-1]//2).item(),
                    torch.sum(sample['dead_lines'] > est_ksp.shape[-1]//2).item()))
            
            # Convert to image domain
            est_img_coils = ifft(est_ksp_padded)
            
            # RSS images
            est_img_rss = torch.sqrt(
                torch.sum(torch.square(torch.abs(est_img_coils)), axis=1))
            
            # Central crop
            est_crop_rss = crop(est_img_rss, 320, 320)
            gt_rss       = sample['ref_rss']
            data_range   = sample['data_range']

            # SSIM loss
            ssim_loss = ssim(est_crop_rss[:, None], gt_rss[:, None],
                             data_range)
            
            # Other losses for tracking
            with torch.no_grad():
                pix_loss  = pixel_loss(est_crop_rss, gt_rss)
                coil_loss = multicoil_loss(est_ksp, sample['gt_nonzero_ksp'])
            
            loss = hparams.ssim_lam * ssim_loss + hparams.coil_lam * coil_loss # For now
           
            # Skip if NaN
            if np.isnan(loss.item()):
                print('Skipping a NaN loss!')
                # Free up as much memory as possible
                del loss, ssim_loss, pix_loss, coil_loss
                del est_crop_rss, gt_rss, data_range
                del est_img_rss, est_img_coils, est_ksp_padded
                del est_img_kernel, est_map_kernel, est_ksp
                del sample
                torch.cuda.empty_cache()
                # Need a dummy loss
                loss = None

                # Reload the previous stable state
                model.load_state_dict(stable_model)
                optimizer.load_state_dict(stable_opt)

                continue

            # Keep a running loss
            running_ssim = 0.99 * running_ssim + 0.01 * (1-ssim_loss.item()) if running_ssim > -1. else (1-ssim_loss.item())
            running_loss = 0.99 * running_loss + 0.01 * pix_loss.item() if running_loss > 0. else pix_loss.item()
            running_coil = 0.99 * running_coil + 0.01 * coil_loss.item() if running_coil > 0. else coil_loss.item()

            loss_log.append(running_loss)
            ssim_log.append(running_ssim)
            coil_log.append(running_coil)
           
            # Save a stable model state
            stable_model = copy.deepcopy(model.state_dict())
            stable_opt   = copy.deepcopy(optimizer.state_dict())

            # Backprop
            optimizer.zero_grad()
            loss.backward()
            # For MoDL (?), clip gradients
            torch.nn.utils.clip_grad_norm(model.parameters(), hparams.grad_clip)
            optimizer.step()
            
            # Save best model
            if running_loss < best_loss:
                best_loss = running_loss
                torch.save({
                    'epoch': epoch_idx,
                    'sample_idx': sample_idx,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'ssim_log': ssim_log,
                    'loss_log': loss_log,
                    'coil_log': coil_log,
                    'loss': loss,
                    'hparams': hparams}, local_dir + '/best_weights.pt')
            
            # Verbose
            print('Epoch %d, Step %d, Batch loss %.4f. Avg. SSIM %.4f, Avg. RSS %.4f, Avg. Coils %.4f' % (
                epoch_idx, sample_idx, loss.item(), running_ssim, running_loss, running_coil))
            
        # Save models
        last_weights = local_dir +'/ckpt_epoch%d.pt' % epoch_idx
        torch.save({
            'epoch': epoch_idx,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'ssim_log': ssim_log,
            'loss_log': loss_log,
            'coil_log': coil_log,
            'loss': loss,
            'hparams': hparams}, last_weights)
        
        # Scheduler
        scheduler.step()
        
        # After each epoch, check some validation samples
        model.eval()
        iterator = iter(val_loader)
        # Plot
        plt.figure()
        for sample_idx in range(4):
            sample = next(iterator)
            # Move to CUDA
            for key, value in sample.items():
                try:
                    sample[key] = sample[key].cuda()
                except:
                    pass
            
            # Get outputs
            with torch.no_grad():
                # Estimate
                est_img_kernel, est_map_kernel, est_ksp = \
                    model(sample, num_unrolls)
                
                # Extra padding with dead zones
                est_ksp_padded = F.pad(est_ksp, (
                    torch.sum(sample['dead_lines'] < est_ksp.shape[-1]//2).item(),
                    torch.sum(sample['dead_lines'] > est_ksp.shape[-1]//2).item()))
                
                # Convert to image domain
                est_img_coils = ifft(est_ksp_padded)
                
                # RSS images
                est_img_rss = torch.sqrt(torch.sum(torch.square(torch.abs(est_img_coils)), axis=1))
                # Central crop
                est_crop_rss = crop(est_img_rss, 320, 320)
                
                # Losses
                ssim_loss = ssim(est_crop_rss[:, None], sample['ref_rss'][:, None],
                                 sample['data_range'])
                l1_loss   = pixel_loss(est_crop_rss, sample['ref_rss'])
                
            # Plot
            plt.subplot(2, 4, sample_idx+1)
            plt.imshow(sample['ref_rss'][0].cpu().detach().numpy(), vmin=0., vmax=0.1, cmap='gray')
            plt.axis('off'); plt.title('GT RSS')
            plt.subplot(2, 4, sample_idx+1+4*1)
            plt.imshow(est_crop_rss[0].cpu().detach().numpy(), vmin=0., vmax=0.1, cmap='gray')
            plt.axis('off'); plt.title('Ours - RSS')
            
        # Save
        plt.tight_layout()
        plt.savefig(local_dir + '/val_samples_epoch%d.png' % epoch_idx, dpi=300)
        plt.close()
        
        # Plot training dynamics
        plt.figure()
        plt.subplot(1, 2, 1); plt.semilogy(loss_log, linewidth=2.); 
        plt.grid(); plt.xlabel('Step'); plt.title('Training pix. loss')
        plt.subplot(1, 2, 2); plt.semilogy(ssim_log, linewidth=2.); 
        plt.grid(); plt.xlabel('Step'); plt.title('Training 1 - SSIM')

        plt.tight_layout()
        plt.savefig(local_dir + '/training_behavior_epoch%d.png' % epoch_idx, dpi=300)
        plt.close()
