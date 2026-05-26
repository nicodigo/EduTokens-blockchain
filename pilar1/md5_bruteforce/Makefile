all: md5_bruteforce

md5_bruteforce: md5_bruteforce.cu md5.cuh
	nvcc -arch=sm_75 md5_bruteforce.cu -o md5_bruteforce

clean:
	rm -f md5_bruteforce
