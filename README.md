## INVIO TOPOLOGIA DA FILE JSON CON:
```bash
curl -X POST http://<node-ip>:30080/pipeline \
     -H "Content-Type: application/json" \
     -d @pipeline.json
```
## CANCELLARE TOPOLOGIE GENERATE
```bash
curl -X DELETE http://<node-ip>:30080/pipeline/pipeline-a1b2c3d4
```
