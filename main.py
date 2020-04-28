import tensorflow as tf
import tensorflow_gan as tfgan
import tensorflow_hub as hub

import numpy as np

from imageio import imwrite
import os
import argparse

from discriminator import Discriminator 
from generator import SPADEGenerator


parser.add_argument('--batch-size', type=int, default=128,
					help='Sizes of image batches fed through the network')

parser.add_argument('--num-data-threads', type=int, default=2,
					help='Number of threads to use when loading & pre-processing training images')

parser.add_argument('--num-epochs', type=int, default=200,
					help='Number of passes through the training data to make before stopping')

parser.add_argument('--gen-learn-rate', type=float, default=0.0001,
					help='Learning rate for Generator Adam optimizer')

parser.add_argument('--dsc-learn-rate', type=float, default=0.0004,
					help='Learning rate for Discriminator Adam optimizer')

parser.add_argument('--beta1', type=float, default=0.5,
					help='"beta1" parameter for Adam optimizer')

parser.add_argument('--beta2', type=float, default=0.999,
					help='"beta2" parameter for Adam optimizer')

parser.add_argument('--log-every', type=int, default=7,
					help='Print losses after every [this many] training iterations')

parser.add_argument('--save-every', type=int, default=500,
					help='Save the state of the network after every [this many] training iterations')

parser.add_argument('--device', type=str, default='GPU:0' if gpu_available else 'CPU:0',
					help='specific the device of computation eg. CPU:0, GPU:0, GPU:1, GPU:2, ... ')

args = parser.parse_args()

## --------------------------------------------------------------------------------------

# Numerically stable logarithm function
def log(x):
	"""
	Finds the stable log of x

	:param x: 
	"""
	return tf.math.log(tf.maximum(x, 1e-5))

## --------------------------------------------------------------------------------------

# For evaluating the quality of generated images
# Frechet Inception Distance measures how similar the generated images are to the real ones
# https://nealjean.com/ml/frechet-inception-distance/
# Lower is better
module = tf.keras.Sequential([hub.KerasLayer("https://tfhub.dev/google/tf2-preview/inception_v3/classification/4", output_shape=[1001])])
def fid_function(real_image_batch, generated_image_batch):
	"""
	Given a batch of real images and a batch of generated images, this function pulls down a pre-trained inception 
	v3 network and then uses it to extract the activations for both the real and generated images. The distance of 
	these activations is then computed. The distance is a measure of how "realistic" the generated images are.

	:param real_image_batch: a batch of real images from the dataset, shape=[batch_size, height, width, channels]
	:param generated_image_batch: a batch of images generated by the generator network, shape=[batch_size, height, width, channels]

	:return: the inception distance between the real and generated images, scalar
	"""
	INCEPTION_IMAGE_SIZE = (299, 299)
	real_resized = tf.image.resize(real_image_batch, INCEPTION_IMAGE_SIZE)
	fake_resized = tf.image.resize(generated_image_batch, INCEPTION_IMAGE_SIZE)
	module.build([None, 299, 299, 3])
	real_features = module(real_resized)
	fake_features = module(fake_resized)
	return tfgan.eval.frechet_classifier_distance_from_activations(real_features, fake_features)

# Train the model for one epoch.
def train(generator, discriminator, dataset_iterator, manager):
	"""
	Train the model for one epoch. Save a checkpoint every 500 or so batches.

	:param generator: generator model
	:param discriminator: discriminator model
	:param dataset_ierator: iterator over dataset, see preprocess.py for more information
	:param manager: the manager that handles saving checkpoints by calling save()

	:return: The average FID score over the epoch
	"""
	# Loop over our data until we run out
	total_fid = 0
	iterations = 0
	for iteration, batch in enumerate(dataset_iterator):
		# TODO: Train the model
		
		with tf.GradientTape() as generator_tape, tf.GradientTape() as discriminator_tape:
			#generate random noise
			noise = tf.random.uniform((args.batch_size, args.z_dim), minval=-1, maxval=1)
			
			#calculate generator output
			gen_output = generator.call(noise)
			
			#Get discriminator output for fake images and real images
			disc_real = discriminator.call(batch)
			disc_fake = discriminator.call(gen_output, noise)
			
			#calculate gen. loss and disc. loss
			g_loss = generator.loss_function(disc_fake)
			d_loss = discriminator.loss_function(disc_real, disc_fake)
			
			#get gradients
			g_grad = generator_tape.gradient(g_loss, generator.trainable_variables)
			d_grad = discriminator_tape.gradient(d_loss, discriminator.trainable_variables)
			
		generator.optimizer.apply_gradients(zip(g_grad, generator.trainable_variables))
		
		# Save
		if iteration % args.save_every == 0:
			manager.save()

		# Calculate inception distance and track the fid in order
		# to return the average
		if iteration % 500 == 0:
			fid_ = fid_function(batch, gen_output)
			total_fid += fid_
			iterations += 1
			print('**** INCEPTION DISTANCE: %g ****' % fid_)
	return total_fid / iterations


# Test the model by generating some samples.
def test(generator):
	"""
	Test the model.

	:param generator: generator model

	:return: None
	"""
	# TODO: Replace 'None' with code to sample a batch of random images
	noise = tf.random.uniform((args.batch_size, args.z_dim), minval=-1, maxval=1)
	img = generator.call(noise).numpy()

	### Below, we've already provided code to save these generated images to files on disk
	# Rescale the image from (-1, 1) to (0, 255)
	img = ((img / 2) - 0.5) * 255
	# Convert to uint8
	img = img.astype(np.uint8)
	# Save images to disk
	for i in range(0, args.batch_size):
		img_i = img[i]
		s = args.out_dir+'/'+str(i)+'.png'
		imwrite(s, img_i)

## --------------------------------------------------------------------------------------

def main():
	# Load a batch of images (to feed to the discriminator)
	dataset_iterator = load_image_batch(args.img_dir, batch_size=args.batch_size, n_threads=args.num_data_threads)

	# Initialize generator and discriminator models
	generator = SPADEGenerator()
	discriminator = Discriminator()

	# For saving/loading models
	checkpoint_dir = './checkpoints'
	checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
	checkpoint = tf.train.Checkpoint(generator=generator, discriminator=discriminator)
	manager = tf.train.CheckpointManager(checkpoint, checkpoint_dir, max_to_keep=3)
	# Ensure the output directory exists
	if not os.path.exists(args.out_dir):
		os.makedirs(args.out_dir)

	if args.restore_checkpoint or args.mode == 'test':
		# restores the latest checkpoint using from the manager
		checkpoint.restore(manager.latest_checkpoint) 

	try:
		# Specify an invalid GPU device
		with tf.device('/device:' + args.device):
			if args.mode == 'train':
				for epoch in range(0, args.num_epochs):
					print('========================== EPOCH %d  ==========================' % epoch)
					avg_fid = train(generator, discriminator, dataset_iterator, manager)
					print("Average FID for Epoch: " + str(avg_fid))
					# Save at the end of the epoch, too
					print("**** SAVING CHECKPOINT AT END OF EPOCH ****")
					manager.save()
			if args.mode == 'test':
				test(generator)
	except RuntimeError as e:
		print(e)

if __name__ == '__main__':
   main()

