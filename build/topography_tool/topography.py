from flask import Flask, request, jsonify
from kubernetes import client, config
import yaml
import uuid
from typing import Union, List, Dict

app = Flask(__name__)

def flatten_steps(steps: List[Union[Dict, List]]):
    flat = []
    for step in steps:
        if isinstance(step, dict):
            next_step = step.get("next_step", [])
            if isinstance(next_step, int):
                next_step = [next_step]
            step_obj = {
                "id": step["step_id"],
                "type": step["type"],
                "params": step.get("params", {}),
                "gpu": step.get("gpu", False),
                "volumes": step.get("volumes", []),
                "priority": step.get("priority",""),
                "preferred_next": step.get("preferred_next"),
                #"next_step": next_step,
                "next_step": step.get("next_step",[]),
                "nodeSelector": step.get("nodeSelector")
            }
            flat.append(step_obj)
            #current_id += 1
        elif isinstance(step, list):
            sub_flat = flatten_steps(step)
            # next_step del step precedente punta al primo sub-step
            #if flat:
            #    flat[-1]["next_step"] = [sub_flat[0]["id"]]
            flat.extend(sub_flat)
            #current_id = flat[-1]["id"] + 1

    # assegna next_step tra gli step principali
    #for i in range(len(flat) - 1):
    #    if not flat[i]["next_step"]:
    #        flat[i]["next_step"] = [flat[i + 1]["id"]]

    return flat



# --- YAML Builders ---
def generate_configmap(step, pipeline_id, namespace="default"):
    # Costruisco lo step che deve andare dentro "steps"
    step_config = {
        "id": step["id"],
        "type": step["type"],
        "params": step.get("params", {}),
        "gpu": step.get("gpu", False),
        "volumes": step.get("volumes", []),
        "priority": step.get("priority",""),
        "preferred_next": step.get("preferred_next"),
        "next_step": step.get("next_step", []),
    }
    # Struttura della configmap
    config_data = {
        "pipeline_id": pipeline_id,
        "step_id": step["id"],
        "steps": [step_config],
    }
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{pipeline_id}-step-{step['id']}",
            "namespace": namespace,
            "labels": {"pipeline_id": pipeline_id},
        },
        "data": {
            "PIPELINE_CONFIG": yaml.dump(config_data, default_flow_style=False)
        },
    }

    return cm
    """return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=f"{pipeline_id}-step-{step['id']}",
            namespace=namespace,
            labels={"pipeline_id": pipeline_id}
        ),
        data={
            "PIPELINE_CONFIG": yaml.dump({
                "pipeline_id": pipeline_id,
                "step_id": step["id"],
                "type": step["type"],
                "params": step.get("params", {}),
                "gpu": step.get("gpu", False),
                "volumes": step.get("volumes", []),
                "priority": step.get("priority",""),
                "preferred_next": step.get("preferred_next"),
                "next_step": step.get("next_step", []),
            }, sort_keys=False)
        }
    )"""


def generate_deployments(steps: List[Dict], pipeline_prefix: str, namespace="default") -> List[Dict]:
    """
    Genera i deployment per ogni step della pipeline.
    """
    deployments = []

    for step in steps:
        step_id = step["id"]
        deployment_name = f"{pipeline_prefix}-step-{step_id}"
        node_selector = step.get("nodeSelector") or {}
        #docker hub images: "dami00/multicomponent_service"
        #if node_selector.get("kubernetes.io/hostname") == "jetsonorigin":
        #    base_image = "192.168.1.252:480/jetson/multicomponent_service:r36"
        #else:
        #    base_image = "192.168.1.252:480/jetson/multicomponent_service:latest"
        base_image = "192.168.1.252:480/jetson/multicomponent_service:latest"
        # container base
        container = {
            "name": "nn",
            "image": base_image,
            "imagePullPolicy": "Always",
            "envFrom": [{"configMapRef": {"name": f"{pipeline_prefix}-step-{step_id}"}}],
            #"env": [
            #    {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}
            #],
            "env": [
                {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
                {"name": "SERVICE_PORT", "value": "5000"},
            ],
            "ports": [{"containerPort": 5000}],
            "readinessProbe": {
                "httpGet": {
                    "path": "/readyz",
                    "port": 5000
                },
                "initialDelaySeconds": 3,
                "periodSeconds": 5
            },
            "lifecycle": {
                "preStop": {
                    "exec": {
                        "command": ["curl", "-X", "POST", "http://localhost:5000/drain"]
                    }
                }
            },
        }

        # GPU
        if step.get("gpu", False):
            container["resources"] = {"requests": {"memory": "512Mi"},"limits": {"nvidia.com/gpu.shared": 1 ,  "memory": "768Mi"}}
        else
            container["resources"] = {"requests": {"memory": "512Mi"},"limits": {"memory": "768Mi"}}

        # Volumes
        volume_mounts = []
        volumes = []
        for vol in step.get("volumes", []):
            if vol == "cuda":
                volume_mounts.append({"mountPath": "/usr/local/cuda", "name": "cuda-volume"})
                volumes.append({"name": "cuda-volume", "hostPath": {"path": "/usr/local/cuda", "type": "Directory"}})
            elif vol == "lib":
                volume_mounts.append({"mountPath": "/usr/lib/aarch64-linux-gnu", "name": "lib-volume"})
                volumes.append({"name": "lib-volume", "hostPath": {"path": "/usr/lib/aarch64-linux-gnu", "type": "Directory"}})
            elif vol == "jetson-inference":
                volume_mounts.append({"mountPath": "/jetson-inference", "name": "jetson-inference-volume"})
                volumes.append({"name": "jetson-inference-volume", "hostPath": {"path": "/home/administrator/jetson-inference", "type": "Directory"}})

        if volume_mounts:
            container["volumeMounts"] = volume_mounts
        # NodeSelector
        #node_selector = step.get("nodeSelector")

        # deployment_spec = {
        #     "replicas": 1,
        #     "selector": {"matchLabels": {"app": "nn-service", "step": str(step_id), "pipeline_id": pipeline_prefix}},
        #     "template": {
        #         "metadata": {"labels": {"app": "nn-service", "step": str(step_id), "pipeline_id": pipeline_prefix}},
        #         "spec": {
        #             "schedulerName": "scheduler-plugins-scheduler",
        #             "containers": [container],
        #             "volumes": volumes if volumes else [],
        #         },
        #     },
        # }
        
        deployment_spec = {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "nn-service", "step": str(step_id), "pipeline_id": pipeline_prefix}},
            "template": {
                "metadata": {"labels": {"app": "nn-service", "step": str(step_id), "pipeline_id": pipeline_prefix},
                            "annotations": {
                                "prometheus.io/scrape": "true",
                                "prometheus.io/port": "5000",
                                "prometheus.io/path": "/metrics",
                            }},
                "spec": {
                    "schedulerName": "scheduler-plugins-scheduler",
                    "containers": [container],
                    "volumes": volumes if volumes else [],
                    "priorityClassName": step.get("priority",""),
                },
            },
        }

        if node_selector:
            deployment_spec["template"]["spec"]["nodeSelector"] = node_selector
        # if str(step_id)==0:
        #     deployment_spec["template"]["metadata"] = {
        #                                                     "labels": {"app": "nn-service", "step": str(step_id), "pipeline_id": pipeline_prefix},
        #                                                     "annotations": {
        #                                                         "prometheus.io/scrape": "true",
        #                                                         "prometheus.io/port": "5000",
        #                                                         "prometheus.io/path": "/metrics",
        #                                                     }
        #                                                 }
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": deployment_name, "namespace": namespace,"labels":{"pipeline_id": pipeline_prefix}},
            "spec": deployment_spec,
        }

        deployments.append(deployment)

    return deployments


def generate_services(steps: List[Dict], pipeline_prefix: str, namespace="default") -> List[Dict]:
    """
    Genera i service per ogni step della pipeline.
    """
    services = []

    for step in steps:
        step_id = step["id"]
        service_name = f"{pipeline_prefix}-step-{step_id}"
        spec = {
            "selector": {"app": "nn-service", "step": str(step_id)},
            "ports": [{"name": "http","port": 5000, "targetPort": 5000}],
        }
        
        if step["id"] == 0:
            spec["type"] = "NodePort"
            spec["ports"] = [{"name": "http", "port": 5000, "targetPort": 5000,"nodePort": 32400}]
        # service = {
        #     "apiVersion": "v1",
        #     "kind": "Service",
        #     "metadata": {"name": service_name, "namespace": namespace,"labels":{"pipeline_id": pipeline_prefix}},
        #     "spec": spec,
        # }
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": namespace,
                "labels": {
                    "pipeline_id": pipeline_prefix,
                    "app": "nn-service" 
                }
            },
            "spec": spec,
        }
        services.append(service)

    return services



# --- Endpoints ---
@app.route("/pipeline", methods=["POST"])
def create_pipeline():
    try:
        pipeline = request.get_json()
        if not pipeline or "steps" not in pipeline:
            return jsonify({"error": "Invalid JSON, must contain steps"}), 400

        config.load_incluster_config()
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        pipeline_id = f"pipeline-{uuid.uuid4().hex[:6]}"
        steps = flatten_steps(pipeline["steps"])
        results = []
        
        # --- Creazione ConfigMap ---
        for step in steps:
            cm = generate_configmap(step, pipeline_id)   # <-- passi solo step e pipeline_id
            v1.create_namespaced_config_map(namespace="default", body=cm)
            results.append(f"✅ ConfigMap creata per step {step['id']}")


        # --- Creazione Deployment ---
        deployments = generate_deployments(steps, pipeline_id)
        for dep in deployments:
            apps_v1.create_namespaced_deployment(namespace="default", body=dep)
            results.append(f"✅ Deployment creato per {dep['metadata']['name']}")

        # --- Creazione Service ---
        services = generate_services(steps, pipeline_id)
        for svc in services:
            v1.create_namespaced_service(namespace="default", body=svc)
            results.append(f"✅ Service creato per {svc['metadata']['name']}")

        return jsonify({"status": "ok", "pipeline_id": pipeline_id, "results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/pipeline/<pipeline_id>", methods=["DELETE"])
def delete_pipeline(pipeline_id):
    try:
        config.load_incluster_config()
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        delete_opts = client.V1DeleteOptions()

        # Cancella tutto con label pipeline_id
        # apps_v1.delete_collection_namespaced_deployment(namespace="default", label_selector=f"pipeline_id={pipeline_id}", body=delete_opts)
        apps_v1.delete_collection_namespaced_deployment(namespace="default",label_selector=f"pipeline_id={pipeline_id}",body=delete_opts)
        v1.delete_collection_namespaced_config_map(namespace="default", label_selector=f"pipeline_id={pipeline_id}", body=delete_opts)
        v1.delete_collection_namespaced_service(namespace="default", label_selector=f"pipeline_id={pipeline_id}", body=delete_opts)

        return jsonify({"status": "deleted", "pipeline_id": pipeline_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
