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

---
