INVIO TOPOLOGIA DA FILE JSON CON:
curl -X POST http://<service-ip>:8080/pipeline \
     -H "Content-Type: application/json" \
     -d @pipeline.json
CANCELLARE TOPOLOGIE GENERATE
curl -X DELETE http://<service-ip>:8080/pipeline/pipeline-a1b2c3d4
