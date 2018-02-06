import os
import random
import time
import torch
import numpy as np
from torch import autograd
from torch.autograd import Variable
from torch.nn import functional as F
from torch.nn.functional import softmax, sigmoid, log_softmax
from torch.nn.functional import nll_loss, cross_entropy
from vector import gen_noise, clamp_to_unit_sphere
import imutil
from imutil import VideoMaker


def to_torch(z, requires_grad=False):
    return Variable(torch.FloatTensor(z), requires_grad=requires_grad).cuda()


def to_np(z):
    return z.data.cpu().numpy()


# Just picks ground-truth examples with the right labels
def rejection_sample(networks, dataloader, **options):
    result_dir = options['result_dir']

    # Generate a K by K square grid of examples, one column per class
    K = dataloader.num_classes
    images = [[] for _ in range(N)]
    for img_batch, label_batch, _ in dataloader:
        for img, label in zip(img_batch, label_batch):
            if label < N and len(images[label]) < N:
                images[label].append(img.cpu().numpy())
        if all(len(images[i]) == N for i in range(N)):
            break

    flat = []
    for i in range(N):
        for j in range(N):
            flat.append(images[j][i])
    images = flat

    images = np.array(images).transpose((0,2,3,1))
    start_class = 0
    video_filename = make_video_filename(result_dir, dataloader, start_class, start_class, label_type='grid')
    # Save the images in npy format to re-load as training data
    trajectory_filename = video_filename.replace('.mjpeg', '.npy')
    np.save(trajectory_filename, images)
    # Save the images in jpg format to display to the user
    imutil.show(images, filename=video_filename.replace('.mjpeg', '.jpg'))
    return images


# Generates 'counterfactual' images for each class, by gradient descent of the class
def generate_counterfactual(networks, dataloader, **options):
    """
    # TODO: Fix Dropout/BatchNormalization outside of training
    for net in networks:
        networks[net].eval()
    """
    result_dir = options['result_dir']

    K = dataloader.num_classes
    # Make the batch size large enough to form a square grid
    cf_count = K + 2

    # Start with randomly-selected images from the dataloader
    start_images, _ = dataloader.get_batch()
    start_images = start_images[:cf_count]  # assume batch_size >= cf_count

    batches = [start_images.cpu().numpy()]
    for target_class in range(K + 1):
        # Generate one column of the visualization, corresponding to a target class
        img_batch = generate_counterfactual_column(networks, start_images, target_class, **options)
        batches.append(img_batch)

    images = []
    for i in range(cf_count):
        for batch in batches:
            images.append(batch[i])

    images = np.array(images).transpose((0,2,3,1))
    dummy_class = 0
    video_filename = make_video_filename(result_dir, dataloader, dummy_class, dummy_class, label_type='grid')

    # Save the images in npy/jpg format as input for the labeling system
    trajectory_filename = video_filename.replace('.mjpeg', '.npy')
    np.save(trajectory_filename, images)
    imutil.show(images, display=False, filename=video_filename.replace('.mjpeg', '.jpg'))

    # Save the images in jpg format to display to the user
    name = 'counterfactual_{}.jpg'.format(int(time.time()))
    jpg_filename = os.path.join(result_dir, 'images', name)
    imutil.show(images, filename=jpg_filename)
    return images


def generate_counterfactual_column(networks, start_images, target_class, **options):
    netG = networks['generator']
    netC = networks['classifier']
    netE = networks['encoder']
    result_dir = options['result_dir']
    latent_size = options['latent_size']
    speed = options['cf_speed']
    max_iters = options['cf_max_iters']
    distance_weight = options['cf_distance_weight']
    gan_scale = options['cf_gan_scale']
    cf_batch_size = len(start_images)

    # Start with the latent encodings
    z_value = to_np(netE(start_images, gan_scale))
    z0_value = z_value

    # Move them so their labels match target_label
    target_label = Variable(torch.LongTensor(cf_batch_size)).cuda()
    target_label[:] = target_class

    for i in range(max_iters):
        z = to_torch(z_value, requires_grad=True)
        z_0 = to_torch(z0_value)
        logits = netC(netG(z, gan_scale))
        augmented_logits = F.pad(logits, pad=(0,1))

        cf_loss = nll_loss(log_softmax(augmented_logits, dim=1), target_label)

        distance_loss = torch.sum(
                (
                    z.mean(dim=-1).mean(dim=-1)
                    -
                    z_0.mean(dim=-1).mean(dim=-1)
                ) ** 2
            ) * distance_weight

        color_loss = torch.sum(
                (
                    netG(z, gan_scale).mean(dim=-1).mean(dim=-1)
                    -
                    Variable(start_images).mean(dim=-1).mean(dim=-1)
                ) ** 2
            ) * 1.0

        total_loss = cf_loss + distance_loss + color_loss

        scores = F.softmax(augmented_logits, dim=1)
        print("Classification {} {:.4f}".format(
            target_class, scores[0][target_class].data[0]))

        print("i={:>4d} t={} cf loss {:.4f}, distance loss {:.4f} color_loss {:.4f} score {:.2f}".format(
            i, target_class, cf_loss.data[0], distance_loss.data[0], color_loss.data[0], scores[0][target_class].data[0]))
        
        dc_dz = autograd.grad(total_loss, z, total_loss)[0]
        z = z - dc_dz * speed
        z = clamp_to_unit_sphere(z, gan_scale)

        # TODO: Workaround for Pytorch memory leak
        # Convert back to numpy and destroy the computational graph
        # See https://github.com/pytorch/pytorch/issues/4661
        z_value = to_np(z)
        del z
    z = to_torch(z_value)

    images = netG(z, gan_scale)
    return images.data.cpu().numpy()


# Trajectories are written to result_dir/trajectories/
def make_video_filename(result_dir, dataloader, start_class, target_class, label_type='active'):
    trajectory_id = '{}_{}'.format(dataloader.dsf.name, int(time.time() * 1000))
    start_class_name = dataloader.lab_conv.labels[start_class]
    target_class_name = dataloader.lab_conv.labels[target_class]
    video_filename = '{}-{}-{}-{}.mjpeg'.format(label_type, trajectory_id, start_class_name, target_class_name)
    video_filename = os.path.join('trajectories', video_filename)
    video_filename = os.path.join(result_dir, video_filename)
    path = os.path.join(result_dir, 'trajectories')
    if not os.path.exists(path):
        print("Creating trajectories directory {}".format(path))
        os.mkdir(path)
    return video_filename
