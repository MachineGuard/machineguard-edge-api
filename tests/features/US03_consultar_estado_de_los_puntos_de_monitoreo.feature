# language: es
@EdgeProcessing @US03
Característica: US03 - Consultar estado de los puntos de monitoreo
  Como Jefe de Almacén
  deseo conocer el estado de conectividad de cada Sensor Node
  para identificar puntos de monitoreo que hayan dejado de reportar información.

  Antecedentes:
    Dado que el gateway Edge tiene registrado el nodo "esp32-01" con un intervalo de muestreo de 60 segundos
    Y que la API central está disponible
    Y que el nodo "esp32-01" envió una lectura capturada hace 0 minutos

  Escenario: Un nodo que reporta dentro del Sampling Interval se considera activo
    Cuando pasan 30 segundos
    Entonces el nodo "esp32-01" tiene el estado "ONLINE"
    Y no hay nodos Offline

  Escenario: Un nodo que deja de reportar se identifica como Offline Node
    Cuando pasan 90 segundos
    Entonces el nodo "esp32-01" aparece entre los nodos Offline
    Y el nodo "esp32-01" tiene el estado "OFFLINE"

  Escenario: Un nodo Offline se restaura al recibir una nueva lectura
    Dado que pasan 90 segundos
    Y que el nodo "esp32-01" aparece entre los nodos Offline
    Cuando el nodo "esp32-01" envía una lectura capturada hace 0 minutos
    Entonces no hay nodos Offline
    Y el nodo "esp32-01" tiene el estado "ONLINE"
