# Pipeline Image Processing App

Questa applicazione Flask implementa una **pipeline di elaborazione immagini** distribuita su Kubernetes, in cui ogni servizio rappresenta uno step della pipeline. L'app legge la configurazione della pipeline da una ConfigMap e passa le immagini da uno step all'altro.

---

## Caratteristiche principali

* Riceve immagini tramite POST `/process`.
* Esegue uno o più step di elaborazione: upscaling, classificazione, grayscale, deblur.
* Registra il tempo di esecuzione di ogni step tramite header custom `X-Step-{ID}-Time`.
* Passa automaticamente le immagini al prossimo step della pipeline usando le ConfigMap di Kubernetes.
* Supporta step preferenziali (`preferred_next`) e selezione dinamica dei prossimi step attivi.
* Logging di eventi INFO e ERROR per monitoraggio.

---

## Configurazione

### Variabili d'ambiente

| Variabile         | Default      | Descrizione                                                                               |
| ----------------- | ------------ | ----------------------------------------------------------------------------------------- |
| `PIPELINE_CONFIG` | `{}`         | YAML della pipeline per questo pod (id pipeline, step, params, next_step, preferred_next) |
| `POD_NAMESPACE`   | `default`    | Namespace Kubernetes dove cercare le ConfigMap                                            |
| `SERVICE_PORT`    | `5000`       | Porta del servizio Flask                                                                  |
| `APP_LABEL`       | `nn-service` | Label del pod/app                                                                         |

### Esempio `PIPELINE_CONFIG`

```yaml
pipeline_id: my-pipeline
step_id: 0
steps:
  - id: 0
    type: upscaling
    params:
      scale: 2
    next_step: 1
    preferred_next: 1
  - id: 1
    type: classifier
    params: {}
    next_step: 2
```

---

## Avvio dell'app

```bash
export PIPELINE_CONFIG='{"pipeline_id":"my-pipeline","step_id":0,"steps":[{"id":0,"type":"upscaling","params":{"scale":2},"next_step":1}]}'
python app.py
```

* L'app espone endpoint `POST /process` per ricevere immagini.
* L'immagine viene elaborata dallo step corrente e inoltrata al prossimo step attivo.

---

## Come funziona

1. L'app legge la configurazione della pipeline dal YAML nella variabile `PIPELINE_CONFIG`.
2. Determina lo **step corrente** (`STEP_ID`) e inizializza l'oggetto step.
3. Quando riceve un'immagine tramite `/process`, la passa a tutti gli step definiti per questo pod.
4. Registra il tempo di esecuzione nello header `X-Step-{ID}-Time`.
5. Determina il **prossimo step** leggendo le ConfigMap attive della pipeline dal namespace.
6. Se esiste un passo preferenziale (`preferred_next`), lo sceglie; altrimenti seleziona il primo step disponibile.
7. Invia l'immagine al servizio del prossimo step tramite HTTP POST.
8. Restituisce il contenuto dell'immagine finale o eventuali errori in formato JSON.

---

##  Nota

* Assicurati che le ConfigMap della pipeline siano presenti e aggiornate.
* L'app funziona all'interno del cluster Kubernetes e richiede permessi per leggere le ConfigMap.
* L'immagine passata deve essere in formato compatibile con PIL (JPEG, PNG, ecc.).

---

## Endpoint

| Endpoint   | Metodo | Descrizione                                                                                                           |
| ---------- | ------ | --------------------------------------------------------------------------------------------------------------------- |
| `/process` | POST   | Riceve un'immagine, esegue lo step corrente, inoltra al prossimo step attivo, restituisce immagine elaborata o errori |

---

## Flusso Pipeline

```text
Client --> /process (step corrente) --> Next step (se presente) --> ... --> Ultimo step --> Risposta al client
```

# Topography Tool

Presente anche un tool per creare automaticamente **pipeline di servizi Kubernetes** composta da ConfigMap, Deployment e Service per ciascun step.

---

## Funzionamento

* Accetta definizione JSON della pipeline via endpoint `/pipeline` (POST).
* Flattening dei step annidati in una lista lineare con ID e next_step.
* Genera automaticamente:

  * **ConfigMap** per ogni step
  * **Deployment** per ogni step
  * **Service** per ogni step (NodePort per il primo step)
* Permette di eliminare l'intera pipeline via endpoint `/pipeline/<pipeline_id>` (DELETE).
* Supporta GPU e montaggio di volumi host (cuda, lib, jetson-inference).

1. Riceve la definizione della pipeline in JSON.
2. Chiama `flatten_steps` per ottenere una lista lineare di step con ID e next_step.
3. Per ogni step:

   * Genera **ConfigMap** con ID, tipo, parametri e next_step.
   * Genera **Deployment** con container configurato e eventuali GPU/volumi.
   * Genera **Service**, con NodePort per il primo step.
4. Le risorse vengono create nel namespace `default` (modificabile).
5. Restituisce uno stato di successo con il `pipeline_id` generato.

Per la cancellazione, elimina tutte le risorse Kubernetes con label `pipeline_id=<pipeline_id>`.

---

### Creazione pipeline

**POST** `/pipeline`
Body JSON:

```json
{
  "steps": [
    {
      "step_id": 0,
      "type": "upscaling",
      "params": {"scale": 2},
      "next_step": 1
    },
    {
      "step_id": 1,
      "type": "classifier",
      "params": {},
      "next_step": 2
    }
  ]
}
```

* Genera ConfigMap, Deployment e Service per ciascun step.
* Restituisce `pipeline_id` unico e lista di risultati.

### Eliminazione pipeline

**DELETE** `/pipeline/<pipeline_id>`

* Cancella tutti i Deployment, ConfigMap e Service associati al `pipeline_id`.

---

## Esempio d'uso

### Creazione pipeline

```bash
curl -X POST http://<node-ip>:30080/pipeline \
  -H 'Content-Type: application/json' \
  -d @pipeline.json
```

### Eliminazione pipeline

```bash
curl -X DELETE http://<node-ip>:30080/pipeline/pipeline-<id>
```

# Load Test con Locust

Questo progetto permette di testare le pipeline esposte come servizi Kubernetes usando **Locust**. È possibile scegliere diverse **curve di carico** e parametri direttamente dalla CLI o via codice.

- Recupera automaticamente gli **endpoint** dei servizi Kubernetes `NodePort` che terminano con `step-0`.
- Invio di immagini (`your_image.jpg`) a tutti gli endpoint.
- Registrazione dei tempi degli step come **metriche personalizzate**.
- Scelta della **curva di carico** e dei parametri da CLI:
  - Ramp
  - Step
  - Spike
  - Sinus
  - Flat

---

## Requisiti

- Locust
- Kubectl configurato per accedere al cluster
- Immagine di test: `your_image.jpg`

Installazione dipendenze:

```bash
pip install locust
```
---


## Struttura file

```text
pipeline_test.py    # Script principale con LocustUser e LoadTestShape
your_image.jpg      # Immagine di test inviata agli endpoint
```

---

## Esempi di utilizzo

### Avvio con curva **Ramp**

```bash
locust -f pipeline_test.py --curve ramp --users 50 --duration 120 --spawn-rate 5
```

### Avvio con curva **Step**

```bash
locust -f pipeline_test.py --curve step --users 50 --duration 120 --spawn-rate 5
```

### Avvio con curva **Spike**

```bash
locust -f pipeline_test.py --curve spike --users 100 --duration 180
```

### Avvio con curva **Sinus**

```bash
locust -f pipeline_test.py --curve sinus --users 30 --duration 60 --spawn-rate 3
```

### Avvio con curva **Flat**

```bash
locust -f pipeline_test.py --curve flat --users 20 --duration 60 --spawn-rate 2
```

---

## Parametri CLI disponibili

| Parametro      | Default | Descrizione                                                       |
| -------------- | ------- | ----------------------------------------------------------------- |
| `--curve`      | ramp    | Tipo di curva di carico: `ramp`, `step`, `spike`, `sinus`, `flat` |
| `--users`      | 20      | Numero massimo di utenti simultanei                               |
| `--duration`   | 60      | Durata totale del test in secondi                                 |
| `--spawn-rate` | 2       | Numero di utenti creati al secondo                                |


---

## Personalizzazione

* Puoi aggiungere nuove curve modificando la classe `CustomShape`.
* Puoi registrare metriche aggiuntive usando `events.request.fire`.
* Supporta qualsiasi numero di endpoint Kubernetes e payload personalizzati.



