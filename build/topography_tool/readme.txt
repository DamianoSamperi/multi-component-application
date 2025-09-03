INVIO TOPOLOGIA DA FILE JSON CON:
curl -X POST http://<pod-ip>:8080/pipeline \
     -H "Content-Type: application/json" \
     -d @pipeline.json
