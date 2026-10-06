---
name: gke-demo-storage
description: >-
  Toy skill for the skill-overlay demo. Covers creating a PersistentVolumeClaim.
---

# GKE Demo Storage

This is a toy skill used by the skill-overlay demo.

## Create a PVC

```bash
kubectl apply -f pvc.yaml
kubectl get pvc
```
