# language: es
@EdgeProcessing @TS03
Característica: TS03 - Mantener lecturas ante interrupciones de conectividad
  Como Developer
  deseo que el Edge Service conserve temporalmente las Measurements cuando no exista comunicación con el servicio central
  para evitar vacíos en el Measurement History.

  Antecedentes:
    Dado que el gateway Edge tiene registrado el nodo "esp32-01" con un intervalo de muestreo de 60 segundos

  Escenario: Sin conexión las mediciones se conservan en el Local Buffer
    Dado que la API central no está disponible
    Cuando el nodo "esp32-01" envía una lectura capturada hace 30 minutos
    Y el nodo "esp32-01" envía una lectura capturada hace 20 minutos
    Y el nodo "esp32-01" envía una lectura capturada hace 10 minutos
    Entonces el Local Buffer tiene 3 mediciones pendientes
    Y se publicaron 0 mediciones en la API central

  Escenario: Al recuperar la conexión se transmiten las mediciones con su marca temporal original
    Dado que la API central no está disponible
    Y que el nodo "esp32-01" envió una lectura capturada hace 10 minutos
    Y que el nodo "esp32-01" envió una lectura capturada hace 30 minutos
    Y que el nodo "esp32-01" envió una lectura capturada hace 20 minutos
    Cuando se restablece la conexión con la API central
    Y el gateway sincroniza el Local Buffer
    Entonces se sincronizan 3 mediciones
    Y el lote enviado conserva las marcas temporales de hace 30, 20 y 10 minutos en orden cronológico
    Y el Local Buffer tiene 0 mediciones pendientes

  Escenario: Una medición ya sincronizada no se vuelve a enviar
    Dado que la API central no está disponible
    Y que el nodo "esp32-01" envió una lectura capturada hace 5 minutos
    Y que se restablece la conexión con la API central
    Y que el gateway sincronizó el Local Buffer
    Cuando el gateway sincroniza el Local Buffer
    Entonces se sincronizan 0 mediciones
    Y la API central recibió 1 lotes

  Escenario: Si la API central sigue caída la sincronización conserva las pendientes
    Dado que la API central no está disponible
    Y que el nodo "esp32-01" envió una lectura capturada hace 5 minutos
    Cuando el gateway sincroniza el Local Buffer
    Entonces se sincronizan 0 mediciones
    Y el Local Buffer tiene 1 mediciones pendientes

  Escenario: Mientras haya pendientes las nuevas lecturas también se encolan
    Dado que la API central no está disponible
    Y que el nodo "esp32-01" envió una lectura capturada hace 5 minutos
    Cuando se restablece la conexión con la API central
    Y el nodo "esp32-01" envía una lectura capturada hace 1 minutos
    Entonces se publicaron 0 mediciones en la API central
    Y el Local Buffer tiene 2 mediciones pendientes

  Escenario: Con el Local Buffer lleno se descartan las pendientes más antiguas
    Dado que el Local Buffer del gateway tiene capacidad para 2 mediciones
    Y que la API central no está disponible
    Y que el nodo "esp32-01" envió una lectura capturada hace 10 minutos
    Y que el nodo "esp32-01" envió una lectura capturada hace 9 minutos
    Y que el nodo "esp32-01" envió una lectura capturada hace 8 minutos
    Entonces el Local Buffer tiene 2 mediciones pendientes
    Y el Local Buffer tiene el estado "FULL"
    Y la medición pendiente más antigua fue capturada hace 9 minutos
