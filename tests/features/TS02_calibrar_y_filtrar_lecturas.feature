# language: es
@EdgeProcessing @TS02
Característica: TS02 - Calibrar y filtrar lecturas en Edge
  Como Developer
  deseo que el Edge Service calibre y filtre las Readings obtenidas por los Sensor Nodes
  para enviar al servicio central Measurements consistentes y confiables.

  Antecedentes:
    Dado que el gateway Edge tiene registrado el nodo "esp32-01" con un intervalo de muestreo de 60 segundos
    Y que la API central está disponible

  Escenario: Se aplica el Calibration Offset a una lectura válida
    Dado que el nodo "esp32-01" tiene un offset de calibración de -0.5 °C y 2 % de humedad
    Cuando el nodo "esp32-01" envía una lectura de 5.0 °C y 60 % de humedad
    Entonces la respuesta tiene el código 201
    Y la medición calibrada es de 4.5 °C y 62 % de humedad
    Y se publicaron 1 mediciones en la API central

  Esquema del escenario: Una lectura fuera del rango físico del sensor se descarta
    Cuando el nodo "esp32-01" envía una lectura de <temperatura> °C y <humedad> % de humedad
    Entonces la respuesta tiene el código 202
    Y la lectura queda con el estado "DISCARDED"
    Y se publicaron 0 mediciones en la API central
    Y hay 1 lecturas descartadas

    Ejemplos:
      | temperatura | humedad |
      | 300         | 60      |
      | -55         | 60      |
      | 5           | 120     |

  Escenario: Cambiar el offset solo afecta a las lecturas posteriores
    Cuando el nodo "esp32-01" envía una lectura de 5.0 °C y 60 % de humedad
    Y se cambia el offset de calibración del nodo "esp32-01" a 1.0 °C y 0 % de humedad
    Y el nodo "esp32-01" envía una lectura de 5.0 °C y 60 % de humedad
    Entonces la medición calibrada es de 6.0 °C y 60 % de humedad
    Y la primera lectura conserva una temperatura calibrada de 5.0 °C

  Escenario: Una lectura de un nodo no registrado es rechazada
    Cuando el nodo "esp32-99" envía una lectura de 5.0 °C y 60 % de humedad
    Entonces la respuesta tiene el código 404
