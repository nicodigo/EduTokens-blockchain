## Hit 2 — Hello World en CUDA

### Entorno de desarrollo

El desarrollo del Pilar 1 se realiza sobre Google Colaboratory, que provee acceso gratuito a GPUs NVIDIA con CUDA preinstalado. Se eligió esta plataforma porque la GPU local disponible (NVIDIA GTX 1060) opera con una arquitectura Pascal (sm_61) incompatible con las versiones modernas del toolkit CUDA.

La sesión de Colab asignó una GPU Tesla T4 (arquitectura Turing, sm_75) con 15360 MiB de memoria de video y un TDP de 70W. El driver instalado es la versión 580.82.07, que soporta hasta CUDA 13.0. El compilador nvcc corresponde al toolkit CUDA 12.8 (build cuda_12.8.r12.8, compilado el 21/02/2025). No existe contradicción entre ambos números: el valor que muestra nvidia-smi es la versión máxima de CUDA soportada por el driver, mientras que nvcc indica la versión del toolkit efectivamente instalado.

El flujo de trabajo adoptado consiste en escribir y versionar el código localmente con asistencia de DeepSeek como herramienta de IA, y luego ejecutarlo en Colab copiando los archivos a la sesión activa. No se presentaron problemas de configuración: el entorno de Colab tiene CUDA disponible sin pasos adicionales de instalación.

### Programa Hello World

El programa implementado lanza un kernel con N threads, donde cada thread imprime su identificador global. Esto verifica que el compilador nvcc funciona correctamente, que el runtime de CUDA puede lanzar kernels, y que la comunicación entre host (CPU) y device (GPU) opera sin errores.

La compilación se realiza con:

```bash
nvcc -arch=sm_75 hello_cuda.cu -o hello_cuda
```

La flag `-arch=sm_75` especifica la arquitectura Turing de la T4. Omitirla produce un binario funcional pero con advertencias de compatibilidad.

[Enlace a notebook de ejemplo](https://colab.research.google.com/drive/1vjTVZpT4vtE9pklU-zulxxQkkooTIhJF?usp=sharing)

---

### Hit 3 — NVIDIA CCCL y Thrust

**CCCL**

NVIDIA CCCL (CUDA Core Compute Libraries) es el repositorio unificado que consolida tres librerías previamente independientes: Thrust, CUB y libcu++. El repositorio se encuentra en desarrollo activo: al momento de redactar este informe, el último commit data de hace 2 horas. El repositorio original de Thrust (`github.com/nvidia/thrust`) fue archivado en marzo de 2024 y toda su actividad migró a CCCL.

**Thrust**

Thrust es una librería de algoritmos paralelos para CUDA con una API modelada sobre la STL de C++. Provee operaciones como ordenamiento, reducción, transformación y búsqueda que se ejecutan en GPU sin requerir que el programador escriba kernels, gestione memoria manualmente ni calcule dimensiones de grillas.

La diferencia práctica con CUDA puro es significativa. En CUDA sin Thrust, ordenar un vector implica escribir o integrar un algoritmo de sort paralelo, gestionar la memoria del device con `cudaMalloc` y `cudaMemcpy`, definir la cantidad de bloques y threads, y sincronizar la ejecución. Con Thrust, la misma operación es `thrust::sort(d_vec.begin(), d_vec.end())`. La librería resuelve todos esos detalles internamente.

La contrapartida es menor control sobre el comportamiento de bajo nivel: distribución de work entre threads, uso de shared memory, y estrategias de scheduling quedan ocultos detrás de la abstracción.

Thrust no requiere instalación adicional: forma parte del toolkit CUDA y está disponible en el entorno de Colab sin ningún paso extra.

**Ejemplo ejecutado**

El programa `pilar1/thrust/thrust_vectors.cu` genera 32 millones de enteros aleatorios en CPU, los transfiere a la GPU, los ordena con `thrust::sort`, y los copia de vuelta al host. Los primeros 5 valores del vector ordenado resultaron:

```
First 5 sorted values: 23 88 106 108 110
```

El orden ascendente confirma que el sort operó correctamente. La compilación se realizó con el Makefile ubicado en `pilar1/thrust/`.

[Enlace a notebook de ejemplo](https://colab.research.google.com/drive/11zPMZA-e8fDbuRSopGlWePngFjcQjt5K?usp=sharing)

---
