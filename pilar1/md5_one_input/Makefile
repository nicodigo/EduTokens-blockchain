all: md5_cuda

md5_cuda: md5_cuda.cu md5.cuh
	nvcc -arch=sm_75 md5_cuda.cu -o md5_cuda

clean:
	rm -f md5_cuda
