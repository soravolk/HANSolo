import os
import torch
from datetime import datetime
import pickle

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from mpl_toolkits.axes_grid1 import make_axes_locatable
from sklearn.utils.class_weight import compute_class_weight
from sacred import Experiment
from sacred.commands import print_config
from sacred.observers import FileStorageObserver
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.nn.utils import clip_grad_norm_
from tqdm import tqdm
from itertools import cycle

from model.UNet import UNet
from model.dataset import prepare_VAT_dataset, compute_dataset_weight
from model.utils import summary, flatten_attention, plot_confusion_matrix
from model.convert import *
from model.evaluate_functions import *
ex = Experiment('train_original')

# parameters for the network
ds_ksize, ds_stride = (2,2),(2,2)
mode = 'imagewise'
sparsity = 2
output_channel = 2
logging_freq = 100 #100
saving_freq = 200

@ex.config
def config():
    root = 'runs'
    device = 'cuda:0'
    log = True
    w_size = 31
    spec = 'Mel'
    resume_iteration = None # 'model-1200'
    train_on = 'Solo'
    n_heads=4
    iteration = 10
    VAT_start = 0
    alpha = 1
    VAT=True
    XI= 1e-6
    eps=1.3 # 2
    reconstruction = False
    batch_size = 8
    train_batch_size = 8
    val_batch_size = 3
    sequence_length = 327680 // 2
    if torch.cuda.is_available() and torch.cuda.get_device_properties(torch.cuda.current_device()).total_memory < 10e9:
        batch_size //= 2
        sequence_length //= 2
        print(f'Reducing batch size to {batch_size} and sequence_length to {sequence_length} to save memory')
    epoches = 8000 # 20000       
    step_size_up = 100    
    max_lr = 1e-4 
    learning_rate = 1e-3
    learning_rate_decay_steps = 1000
    learning_rate_decay_rate = 0.98
    clip_gradient_norm = 3
    validation_length = sequence_length
    refresh = False
    logdir = f'{root}/Unet_Onset-recons={reconstruction}-XI={XI}-eps={eps}-alpha={alpha}-train_on={train_on}-w_size={w_size}-n_heads={n_heads}-lr={learning_rate}-'+ datetime.now().strftime('%y%m%d-%H%M%S')
        
    ex.observers.append(FileStorageObserver.create(logdir)) # saving source code

def tensorboard_log(batch_visualize, model, valid_set, supervised_loader,
                    ep, logging_freq, saving_freq, n_heads, logdir, w_size, writer,
                    VAT, VAT_start, reconstruction):
    technique_dict = {
        0: 'no tech',
        1: 'normal', 
        2: 'slide',
        3: 'bend',
        4: 'trill',
        5: 'mute',
        6: 'pull',
        7: 'harmonic',
        8: 'hammer',
        9: 'tap'
    }
    # log various result from the validation audio
    model.eval()

    if ep%logging_freq==0 or ep==1:
        # on valid set
        with torch.no_grad():
            mertics, cm_dict = evaluate_prediction(valid_set, model, reconstruction=reconstruction)
            for key, values in mertics.items():
                if key.startswith('metric/'):
                    _, category, tech, name = key.split('/')
                    # show metrics on terminal
                    print(f'{category:>32} {tech:12} {name:13}: {np.mean(values):.3f} ± {np.std(values):.3f}')
                    if ('precision' in name or 'recall' in name or 'f1' in name) and 'chroma' not in name:
                        writer.add_scalar(key, np.mean(values), global_step=ep)

        # test on labelled training set
        model.eval()
        test_losses = eval_model(model, ep, supervised_loader, VAT_start, VAT)
        for key, values in test_losses.items():
            if key.startswith('loss/'):
                writer.add_scalar(key, np.mean(values), global_step=ep)

    # visualized validation audio
    predictions, losses, mel = model.run_on_batch(batch_visualize, None, VAT)
    loss = sum(losses.values())

    mel = mel[:,0,:,:]
    
    # Show the original transcription and spectrograms
    if ep==1:
        # spectrogram
        fig, axs = plt.subplots(2, 2, figsize=(24,8))
        axs = axs.flat
        for idx, i in enumerate(mel.cpu().detach().numpy()):
            axs[idx].imshow(i.transpose()) # , cmap='jet', origin='lower'
            axs[idx].axis('off')
        fig.tight_layout()
        writer.add_figure('images/Original', fig , ep)

        # technique ground truth
        fig, axs = plt.subplots(2, 2, figsize=(24,4))
        axs = axs.flat
        # batch_visualize['technique'].shape: [3, 232]
        for idx, i in enumerate(batch_visualize['technique'].unsqueeze(1).cpu().numpy()):
            axs[idx].imshow(i, origin='lower', vmax=1, vmin=0)
            axs[idx].axis('off')
        fig.tight_layout()
        writer.add_figure('images/Label', fig , ep)
        
        # when the spectrogram adds adversarial direction
        if predictions['r_adv'] is not None: 
            fig, axs = plt.subplots(2, 2, figsize=(24,8))
            axs = axs.flat
            for idx, i in enumerate(mel.cpu().detach().numpy()):
                x_adv = i.transpose()+predictions['r_adv'][idx][0].t().cpu().numpy()
                axs[idx].imshow(x_adv, vmax=1, vmin=0)
                axs[idx].axis('off')
            fig.tight_layout()

            writer.add_figure('images/Spec_adv', fig , ep)           
    # Show the training result every period of epoch
    if ep%logging_freq == 0:
        for output_key in ['technique', 'technique2']:
            if output_key in predictions.keys():
                fig, axs = plt.subplots(2, 2, figsize=(24,4))
                axs = axs.flat
                tech_pred = predictions[output_key].detach().cpu()
                tech_pred = tech_pred.unsqueeze(1).numpy() # (3, 232) -> (3, 1, 232)

                for idx, i in enumerate(tech_pred):
                    axs[idx].imshow(i, origin='lower', vmax=1, vmin=0)
                    axs[idx].axis('off')
                fig.tight_layout()
                writer.add_figure(f'images/{output_key}', fig , ep)
        
        for output_key in ['cm', 'Recall', 'Precision', 'cm_2', 'Recall_2', 'Precision_2']:
            if output_key in cm_dict.keys():
                if output_key in ['cm', 'cm_2']:
                    plot_confusion_matrix(cm_dict[output_key], technique_dict, writer, ep, output_key, f'images/{output_key}', 'd', 10)
                else:
                    plot_confusion_matrix(cm_dict[output_key], technique_dict, writer, ep, output_key, f'images/{output_key}', '.2f', 6)

        if 'reconstruction' in predictions.keys():
            fig, axs = plt.subplots(2, 2, figsize=(24,8))
            axs = axs.flat
            for idx, i in enumerate(predictions['reconstruction'].cpu().detach().numpy().squeeze(1)):
                axs[idx].imshow(i.transpose())
                axs[idx].axis('off')
            fig.tight_layout()
            writer.add_figure('images/Reconstruction', fig , ep)                     

        # show adversarial samples    
        if predictions['r_adv'] is not None: 
            fig, axs = plt.subplots(2, 2, figsize=(24,8))
            axs = axs.flat
            for idx, i in enumerate(mel.cpu().detach().numpy()):
                x_adv = i.transpose()+predictions['r_adv'][idx][0].t().cpu().numpy()
                axs[idx].imshow(x_adv, vmax=1, vmin=0, cmap='jet', origin='lower')
                axs[idx].axis('off')
            fig.tight_layout()
            writer.add_figure('images/Spec_adv', fig , ep)            

        # show attention    
        if 'attention' in predictions.keys():
            fig = plt.figure(figsize=(90, 45))
            # Creating the grid for 2 attention head for the transformer
            outer = gridspec.GridSpec(2, 4, wspace=0.2, hspace=0.2)
            fig.suptitle("Visualizing Attention Heads", size=20)
            attentions = predictions['attention']

            for i in range(n_heads):
                # Creating the grid for 4 samples
                inner = gridspec.GridSpecFromSubplotSpec(2, 2,
                                subplot_spec=outer[i], wspace=0.1, hspace=0.1)
                ax = plt.Subplot(fig, outer[i])
                ax.set_title(f'Head {i}', size=20) # This does not show up
                for idx in range(predictions['attention'].shape[0]):
                    axCenter = plt.Subplot(fig, inner[idx])
                    fig.add_subplot(axCenter)
                    attention = attentions[idx, :, i]
                    attention = flatten_attention(attention, w_size)
                    axCenter.imshow(attention.cpu().detach(), cmap='jet')


                    attended_features = mel[idx]

                    # Create another plot on top and left of the attention map                    
                    divider = make_axes_locatable(axCenter)
                    axvert = divider.append_axes('left', size='30%', pad=0.5)
                    axhoriz = divider.append_axes('top', size='20%', pad=0.25)
                    axhoriz.imshow(attended_features.t().cpu().detach(), aspect='auto', origin='lower', cmap='jet')
                    axvert.imshow(tech_pred[idx], aspect='auto')

                    # changing axis for the center fig
                    axCenter.set_xticks([])

                    # changing axis for the output fig (left fig)
                    axvert.set_yticks([])
                    axvert.xaxis.tick_top()
                    axvert.set_title('Transcription')

                    axhoriz.set_title(f'Attended Feature (Spec)')

                    axhoriz.margins(x=0)
                    axvert.margins(y=0)
        writer.add_figure('images/Attention', fig , ep) 

def train_VAT_model(model, iteration, ep, l_loader, ul_loader, optimizer, scheduler, clip_gradient_norm, alpha, VAT=False, VAT_start=0, class_weights=None):
    model.train()
    batch_size = l_loader.batch_size
    total_loss = 0
    l_loader = cycle(l_loader)
    if ul_loader:
        ul_loader = cycle(ul_loader)
    for i in tqdm(range(iteration)):
        optimizer.zero_grad()
        batch_l = next(l_loader)
        
        if (ep < VAT_start) or (VAT==False):
            predictions, losses, _ = model.run_on_batch(batch_l, None, False, class_weights)
        else:
            batch_ul = next(ul_loader)
            predictions, losses, _ = model.run_on_batch(batch_l, batch_ul, VAT, class_weights)

        loss = 0
        # tweak the loss
        # loss = losses(label) + losses(recon) + alpha*(losses['loss/train_LDS_l']+losses['loss/train_LDS_ul'])/2
        # alpha = 1 in the original paper
        for key in losses.keys():
            if key.startswith('loss/train_LDS'):
                loss += alpha*losses[key]/2  # No need to divide by 2 if you have only _l -> ? but you divide both...
            else:
                loss += losses[key]
  
        loss.backward()
        total_loss += loss.item()

        optimizer.step()
        scheduler.step()

        if clip_gradient_norm:
            clip_grad_norm_(model.parameters(), clip_gradient_norm)
        print(f'Train Epoch: {ep} [{i*batch_size}/{iteration*batch_size}'
                f'({100. * i / iteration:.0f}%)]'
                f"\tMain Loss: {sum(losses.values()):.6f}\t"
#                 + f"".join([f"{k.split('/')[-1]}={v.item():.3e}\t" for k,v in losses.items()])
                , end='\r') 
    print(' '*100, end = '\r')          
    print(f'Train Epoch: {ep}\tLoss: {total_loss/iteration:.6f}')
    return predictions, losses, optimizer

@ex.automain
def train(spec, resume_iteration, batch_size, sequence_length, w_size, n_heads, train_batch_size, val_batch_size,
          learning_rate, learning_rate_decay_steps, learning_rate_decay_rate, alpha,
          clip_gradient_norm, refresh, device, epoches, logdir, log, iteration, VAT_start, VAT, XI, eps,
          reconstruction): 
    print_config(ex.current_run)

    supervised_set, unsupervised_set, valid_set, test_set = prepare_VAT_dataset(
                                                                          sequence_length=sequence_length,
                                                                          validation_length=sequence_length,
                                                                          refresh=refresh,
                                                                          device=device,
                                                                          audio_type='flac')  
    if VAT:
        unsupervised_loader = DataLoader(unsupervised_set, batch_size, shuffle=True, drop_last=True)
#     supervised_set, unsupervised_set = torch.utils.data.random_split(dataset, [100, 39],
#                                                                      generator=torch.Generator().manual_seed(42))
    
    # get weight for BCE loss
    class_weights = compute_dataset_weight(device)

    print("supervised_set: ", len(supervised_set))
    print("unsupervised_set: ", len(unsupervised_set))
    print("valid_set: ", len(valid_set))
    print("test_set: ", len(test_set))
    supervised_loader = DataLoader(supervised_set, train_batch_size, shuffle=True, drop_last=True)
    val_loader = DataLoader(valid_set, val_batch_size, shuffle=False, drop_last=True)
    batch_visualize = next(iter(val_loader)) # Getting one fixed batch for visualization   

    # model setting
    ds_ksize, ds_stride = (2,2),(2,2)     
    model = UNet(ds_ksize,ds_stride, log=log, reconstruction=reconstruction,
                    mode=mode, spec=spec, device=device, XI=XI, eps=eps)
    model.to(device)
    if resume_iteration is None:  
        optimizer = torch.optim.Adam(model.parameters(), learning_rate)
        resume_iteration = 0
    else: # Loading checkpoints and continue training
        model_path = os.path.join('checkpoint', f'{resume_iteration}.pt')
        model.load_state_dict(torch.load(model_path))
        optimizer = torch.optim.Adam(model.parameters(), learning_rate)
        optimizer.load_state_dict(torch.load(os.path.join('checkpoint', 'last-optimizer-state.pt')))

    summary(model)
    # scheduler = torch.optim.lr_scheduler.CyclicLR(optimizer, base_lr=base_lr, max_lr=max_lr, step_size_up=step_size_up,cycle_momentum=False)
    scheduler = StepLR(optimizer, step_size=learning_rate_decay_steps, gamma=learning_rate_decay_rate)

    for ep in tqdm(range(1, epoches+1)):
        if VAT==True:
            predictions, losses, optimizer = train_VAT_model(model, iteration, ep, supervised_loader, unsupervised_loader,
                                                             optimizer, scheduler, clip_gradient_norm, alpha, VAT, VAT_start, class_weights)
        else:
            predictions, losses, optimizer = train_VAT_model(model, iteration, ep, supervised_loader, None,
                                                             optimizer, scheduler, clip_gradient_norm, alpha, VAT, VAT_start, class_weights)            
        loss = sum(losses.values())

        # Logging results to tensorboard
        if ep == 1:
            writer = SummaryWriter(logdir) # create tensorboard logger     
        if ep < VAT_start or VAT == False:
            tensorboard_log(batch_visualize, model, valid_set, supervised_loader,
                            ep, logging_freq, saving_freq, n_heads, logdir, w_size, writer,
                            False, VAT_start, reconstruction)
        else:
            tensorboard_log(batch_visualize, model, valid_set, supervised_loader,
                            ep, logging_freq, saving_freq, n_heads, logdir, w_size, writer,
                            True, VAT_start, reconstruction)            

        # Saving model
        if (ep)%saving_freq == 0:
            torch.save(model.state_dict(), os.path.join('checkpoint', f'model-{ep}.pt'))
            torch.save(optimizer.state_dict(), os.path.join('checkpoint', 'last-optimizer-state.pt'))
        
        for key, value in {**losses}.items():
            writer.add_scalar(key, value.item(), global_step=ep) 

    """
    # Evaluating model performance on the full MAPS songs in the test split     
    print('Training finished, now evaluating on the MAPS test split (full songs)')
    with torch.no_grad():
        model = model.eval()
        metrics = evaluate_wo_prediction(tqdm(full_validation), model, reconstruction=False,
                                       save_path=os.path.join(logdir,'./MIDI_results'))
        
    for key, values in metrics.items():
        if key.startswith('metric/'):
            _, category, name = key.split('/')
            print(f'{category:>32} {name:25}: {np.mean(values):.3f} ± {np.std(values):.3f}')
         
    export_path = os.path.join(logdir, 'result_dict')    
    pickle.dump(metrics, open(export_path, 'wb'))
    """