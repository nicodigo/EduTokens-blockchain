#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cstdint>
#include <cuda_runtime.h>
#include "md5.cuh"

// One-thread kernel: process all blocks and write final digest (little-endian).
__global__ void md5Kernel(const uint8_t* padded_msg, int num_blocks, uint8_t* digest) {
    // MD5 initialisation vector
    uint32_t state[4] = {
        0x67452301,  // A
        0xEFCDAB89,  // B
        0x98BADCFE,  // C
        0x10325476   // D
    };

    for (int i = 0; i < num_blocks; i++) {
        md5_transform(state, padded_msg + i * 64);
    }

    // Store each 32-bit word in little-endian byte order
    for (int i = 0; i < 4; i++) {
        digest[i*4]     = (uint8_t)(state[i] & 0xFF);
        digest[i*4 + 1] = (uint8_t)((state[i] >> 8) & 0xFF);
        digest[i*4 + 2] = (uint8_t)((state[i] >> 16) & 0xFF);
        digest[i*4 + 3] = (uint8_t)((state[i] >> 24) & 0xFF);
    }
}

int main(int argc, char** argv) {
    if (argc != 2) {
        fprintf(stderr, "Usage: %s <string>\n", argv[0]);
        return 1;
    }

    const char* input = argv[1];
    size_t input_len = strlen(input);

    // ---- MD5 padding (RFC 1321 §3.1) ----
    // 1. Append bit '1' (byte 0x80)
    // 2. Append zero bytes until length ≡ 56 (mod 64)
    // 3. Append original bit-length as little-endian uint64

    size_t new_len = input_len + 1;         // after 0x80
    size_t rem     = new_len % 64;

    if (rem > 56) {
        new_len += (64 - rem) + 56;         // fill current block + 56 zeros in next
    } else {
        new_len += (56 - rem);               // pad within current block
    }
    new_len += 8;                            // 8-byte length field
    int num_blocks = (int)(new_len / 64);

    uint8_t* padded = (uint8_t*)calloc(new_len, 1);
    if (!padded) { fprintf(stderr, "host alloc failed\n"); return 1; }

    memcpy(padded, input, input_len);
    padded[input_len] = 0x80;

    uint64_t bit_len = input_len * 8;
    for (int i = 0; i < 8; i++) {
        padded[new_len - 8 + i] = (uint8_t)((bit_len >> (i * 8)) & 0xFF);
    }

    // ---- GPU work ----
    uint8_t *d_msg, *d_digest;
    cudaMalloc(&d_msg,    new_len);
    cudaMalloc(&d_digest, 16);

    cudaMemcpy(d_msg, padded, new_len, cudaMemcpyHostToDevice);

    md5Kernel<<<1, 1>>>(d_msg, num_blocks, d_digest);
    cudaDeviceSynchronize();

    uint8_t digest[16];
    cudaMemcpy(digest, d_digest, 16, cudaMemcpyDeviceToHost);

    // ---- Output ----
    for (int i = 0; i < 16; i++) {
        printf("%02x", digest[i]);
    }
    printf("\n");

    // ---- Cleanup ----
    free(padded);
    cudaFree(d_msg);
    cudaFree(d_digest);

    return 0;
}
