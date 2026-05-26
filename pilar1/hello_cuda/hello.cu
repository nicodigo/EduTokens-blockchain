#include <stdio.h>

__global__ void helloKernel() {
    printf("Hola desde GPU - bloque %d, thread %d\n", blockIdx.x, threadIdx.x);
}

int main() {
    printf("Hola desde CPU\n");

    helloKernel<<<2, 4>>>();
    cudaDeviceSynchronize();

    return 0;
}
