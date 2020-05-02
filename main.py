import tensorflow as tf
import tensorflow_gan as tfgan
import tensorflow_hub as hub

# Tensorflow GAN stuff for FID
from keras.applications.inception_v3 import InceptionV3 
from keras.applications.inception_v3 import preprocess_input

import matplotlib.pyplot as plt

import numpy as np
import scipy

from imageio import imwrite
from skimage.io import imsave
from skimage import img_as_ubyte
import os
import csv
import argparse

from code.discriminator import Discriminator 
from code.generator import SPADEGenerator
from preprocess import load_image_batch

# Killing optional CPU driver warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

gpu_available = tf.test.is_gpu_available()
print("GPU Available: ", gpu_available)
EPOCH_COUNT = 0

## --------------------------------------------------------------------------------------
parser = argparse.ArgumentParser(description='GAUGAN')

parser.add_argument('--train-img-dir', type=str, default='./data/landscape_data/train',
					help='Data where training images live')

parser.add_argument('--test-img-dir', type=str, default='./data/landscape_data/test',
					help='Data where test images live')

parser.add_argument('--out-dir', type=str, default='./output',
					help='Data where sampled output images will be written')

parser.add_argument('--mode', type=str, default='train',
					help='Can be "train" or "test"')

parser.add_argument('--restore-checkpoint', action='store_true',
					help='Use this flag if you want to resuming training from a previously-saved checkpoint')

parser.add_argument('--z-dim', type=int, default=64,
					help='Dimensionality of the latent space')

parser.add_argument('--batch-size', type=int, default=16,
					help='Sizes of image batches fed through the network')

parser.add_argument('--num-data-threads', type=int, default=8,
					help='Number of threads to use when loading & pre-processing training images')

parser.add_argument('--num-epochs', type=int, default=200,
					help='Number of passes through the training data to make before stopping')

parser.add_argument('--gen-learn-rate', type=float, default=0.0002,
					help='Learning rate for Generator Adam optimizer')

parser.add_argument('--dsc-learn-rate', type=float, default=0.0003,
					help='Learning rate for Discriminator Adam optimizer')

parser.add_argument('--beta1', type=float, default=0,
					help='"beta1" parameter for Adam optimizer')

parser.add_argument('--beta2', type=float, default=0.999,
					help='"beta2" parameter for Adam optimizer')
					
parser.add_argument('--img-h', type=int, default=96,
					help='height of image')
					
parser.add_argument('--img-w', type=int, default=128,
					help='width of image')

parser.add_argument('--lambda-vgg', type=float, default= 1,#0.01,
					help='weight of vgg loss in generator')

parser.add_argument('--log-every', type=int, default=7,
					help='Print losses after every [this many] training iterations')

parser.add_argument('--save-every', type=int, default=5,
					help='Save the state of the network after every [this many] epochs iterations')

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
# FID Functions adapted from https://machinelearningmastery.com/how-to-implement-the-frechet-inception-distance-fid-from-scratch/
# Lower is better
#module = tf.keras.Sequential([hub.KerasLayer("https://tfhub.dev/google/tf2-preview/inception_v3/classification/4", output_shape=[1001])])
model = InceptionV3(include_top=False, pooling='avg', input_shape=(96,128,3))
def fid_function(real_image_batch, generated_image_batch):
	"""
	Given a batch of real images and a batch of generated images, this function pulls down a pre-trained inception 
	v3 network and then uses it to extract the activations for both the real and generated images. The distance of 
	these activations is then computed. The distance is a measure of how "realistic" the generated images are.
	:param real_image_batch: a batch of real images from the dataset, shape=[batch_size, height, width, channels]
	:param generated_image_batch: a batch of images generated by the generator network, shape=[batch_size, height, width, channels]
	:return: the inception distance between the real and generated images, scalar
	"""
	def calculate_fid(model, img, img2):
		act1 = model.predict(img, steps=1)
		act2 = model.predict(img2, steps=1)

		# calculate mean and covariance statistics
		mu1, sigma1 = act1.mean(axis=0), np.cov(act1, rowvar=False)
		mu2, sigma2 = act2.mean(axis=0), np.cov(act2, rowvar=False)

		# calculate sum squared difference between means
		ssdiff = np.sum((mu1 - mu2)**2.0)

		# calculate sqrt of product between cov
		covmean = scipy.linalg.sqrtm(sigma1.dot(sigma2))

		# check and correct imaginary numbers from sqrt
		if np.iscomplexobj(covmean):
			covmean = covmean.real

		# calculate score
		fid = ssdiff + np.trace(sigma1 + sigma2 - (2.0 * covmean))
		return fid

	real = preprocess_input(real_image_batch)
	gen = preprocess_input(generated_image_batch)

	tot_fid = calculate_fid(model, real, gen)
	
	return tot_fid

# Train the model for one epoch.
def train(generator, discriminator, dataset_iterator, manager):
	"""
	Train the model for one epoch. Save a checkpoint every 500 or so batches.
	:param generator: generator model
	:param discriminator: discriminator model
	:param dataset_iterator: iterator over dataset, see preprocess.py for more information
	:param manager: the manager that handles saving checkpoints by calling save()
	:return: The average FID score over the epoch
	"""
	# Loop over our data until we run out
	total_fid = 0
	total_gen_loss = 0
	total_disc_loss =0
	iterations = 0

	# print("dataset_iterator is ", dataset_iterator)
	# print(dataset_iterator[0])

	for iteration, batch in enumerate(dataset_iterator):
		# Break batch up into images and segmaps 
		images, seg_maps = batch

		with tf.GradientTape() as generator_tape, tf.GradientTape() as discriminator_tape:
			noise = tf.random.uniform((args.batch_size, 256), minval=-1, maxval=1)

			# calculate generator output
			gen_output = generator.call(noise, seg_maps)
			#print("GENERATED ARRAY MIN: ", np.min(gen_output))
			#print("GENERATED ARRAY MAX: ", np.max(gen_output))
			
			# Get discriminator output for fake images and real images
			disc_real = discriminator.call(images, seg_maps)
			disc_fake = discriminator.call(gen_output, seg_maps)
			
			# calculate gen. loss and disc. loss
			g_loss = generator.loss(disc_fake, gen_output, images)
			d_loss = discriminator.loss(disc_real, disc_fake)
			
			# Update loss counters
			total_gen_loss += g_loss
			total_disc_loss += d_loss
			
			# FIXME: For testing purposes to see what generated output looks like
			global EPOCH_COUNT
			if iteration == 0: 
				s = "logs/generated_samples"+'/'+str(EPOCH_COUNT)+'.png'
				img_i = gen_output[0] * 255
				imwrite(s, img_i)

				# plt.figure(1)
				# for n in range(16):
				# 	ax = plt.subplot(4, 4, n+1)
				# 	plt.imshow(gen_output[n])
				# 	plt.axis('off')
				# plt.savefig(s)
				

		# get gradients
		g_grad = generator_tape.gradient(g_loss, generator.trainable_variables)
		d_grad = discriminator_tape.gradient(d_loss, discriminator.trainable_variables)
			
		generator.optimizer.apply_gradients(zip(g_grad, generator.trainable_variables))
		discriminator.optimizer.apply_gradients(zip(d_grad, discriminator.trainable_variables))
	

		# Calculate inception distance and track the fid in order
		# to return the average
		if iteration % 500 == 0:
			fid_ = fid_function(images, gen_output)
			total_fid += fid_
			iterations += 1

	EPOCH_COUNT += 1
	return total_fid / iterations, total_gen_loss / iterations, total_disc_loss / iterations


# Test the model by generating some samples.
def test(generator, dataset_iterator):
	"""
	Test the model.
	:param generator: generator model
	:return: None
	"""
	print("Start Testing")
	total_fid = 0
	total_gen_loss = 0
	total_disc_loss = 0
	iterations = 0
	
	for iteration, batch in enumerate(dataset_iterator):
		noise = tf.random.uniform((args.batch_size, 256), minval=-1, maxval=1)
		image, seg_map = batch
		img = generator.call(noise, seg_map).numpy()

		# Rescale the image from (-1, 1) to (0, 255)
		img = img * 255
		
		if iteration == 1: 
			print(img)

		# Convert to uint8
		#img = img.astype(np.uint8)
		img_i = img[0]
		# Save images to disk
		#img_i = img_as_ubyte(img[0])
		s = args.out_dir+'/'+str(iteration)+'_generated.png'
		s2 = args.out_dir+'/'+str(iteration)+'_truth.png'
		s3 = args.out_dir+'/'+str(iteration)+'_segmap.png'
		imwrite(s, img_i)
		imsave(s2, image[0])

		iterations += 1
	
	return total_fid / iterations, total_gen_loss / iterations, total_disc_loss / iterations
	
## --------------------------------------------------------------------------------------

def main():
	# Load train images (to feed to the discriminator)

	train_dataset_iterator = load_image_batch(dir_name=args.train_img_dir, batch_size=args.batch_size, \
		n_threads=args.num_data_threads)
	
	# Get number of train images and make an iterator over it
	test_dataset_iterator = load_image_batch(dir_name=args.test_img_dir, batch_size=1, \
		n_threads=args.num_data_threads, drop_remainder=False)

	# Initialize generator and discriminator models
	generator = SPADEGenerator(args.beta1, args.beta2, args.gen_learn_rate, \
		args.batch_size, args.z_dim, args.img_w, args.img_h, args.lambda_vgg)
	discriminator = Discriminator(args.beta1, args.beta2, args.dsc_learn_rate)

	print('========================== GENERATOR ==========================')
	# Charlie arbitrarily put 4x4 for the input dims just to see if the code would run
	# Error message: 
	''''
	  File "/gpfs/main/home/cgagnon1/course/cs1430/gaugan/James_TompGAN/code/generator.py", line 34, in call
    result_dense = self.dense(noise)

    ValueError: Input 0 of layer dense is incompatible with the layer: 
    : expected min_ndim=2, found ndim=1. Full shape received: [2]

	'''
	# generator.build(input_shape=(4,4))
	# generator.summary()
	print('========================== DISCRIMINATOR ==========================')
	# discriminator.summary()

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
		print("MOO")
		checkpoint.restore(manager.latest_checkpoint) 

	try:
		# Specify an invalid GPU device
		with tf.device('/device:' + args.device):
			if args.mode == 'train':
				for epoch in range(0, args.num_epochs):
					print('\n')
					print('========================== EPOCH %d  ==========================' % epoch)
					avg_fid, avg_g_loss, avg_d_loss = train(generator, discriminator, train_dataset_iterator, manager)
					print("Average FID for Epoch: ", float(avg_fid))
					print("Average Generator Loss: ", float(avg_g_loss))
					print("Average Discriminator Loss: ", float(avg_d_loss))

					# Save at the end of the epoch, too
					if epoch % args.save_every == 0:
						print("**** SAVING CHECKPOINT AT END OF EPOCH ****")
						manager.save()
					
					# Save the losses and fid into a CSV that we make.
					logs_path = "logs"
					fn = "fid_losses_train.csv"
					full_path = logs_path + '/' + fn
					
					# Make logs directory if it does not exist
					if (not os.path.exists(logs_path)):
						os.mkdir(logs_path)

					# If first Epoch create new file
					if epoch == 0:
						with open(full_path, 'w') as csvfile:
							csvwriter = csv.writer(csvfile)

							# Write the categories and first epoch info
							csvwriter.writerow(['Epoch Num', 'Average FID', 'Average Generator Loss', 'Average Discriminator Loss'])
							csvwriter.writerow([epoch, float(avg_fid), float(avg_g_loss), float(avg_d_loss)])
					else:
						# If any other epoch, append
						with open(full_path, 'a+') as csvfile:  
							csvwritter = csv.writer(csvfile)

							# Write epoch information
							csvwritter.writerow([epoch, float(avg_fid), float(avg_g_loss), float(avg_d_loss)])

			if args.mode == 'test':
				avg_fid, avg_g_loss, avg_d_loss = test(generator, test_dataset_iterator)

				# Save the losses and fid into a CSV that we make.
				logs_path = "logs"
				fn = "fid_losses_test.csv"
				full_path = logs_path + '/' + fn
				
				# Make logs directory if it does not exist
				if (not os.path.exists(logs_path)):
					os.mkdir(logs_path)

				# If first Epoch create new file
				with open(full_path, 'w') as csvfile:
					csvwriter = csv.writer(csvfile)

					# Write the categories and first epoch info
					csvwriter.writerow(['Average FID', 'Average Generator Loss', 'Average Discriminator Loss'])
					csvwriter.writerow([avg_fid, avg_g_loss, avg_d_loss])

	except RuntimeError as e:
		print(e)
	
if __name__ == '__main__':
	main()


