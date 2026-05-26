all: thrust

thrust: thrust_vectors.cu
	nvcc -arch=sm_75 thrust_vectors.cu -o thrust_vectors

clean:
	rm -f thrust_vectors
