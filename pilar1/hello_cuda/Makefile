all: hello

hello: hello.cu
	nvcc -arch=sm_75 hello.cu -o hello

clean:
	rm -f hello
