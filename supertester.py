from code.spadelayer import SpadeLayer  
import tensorflow as tf
#import 

random = tf.random.normal((1,1))

layers_of_onion = 10
onion_layer = SpadeLayer(layers_of_onion)

# Do stuff
onion_layer(random)

print(onion_layer.losses)
