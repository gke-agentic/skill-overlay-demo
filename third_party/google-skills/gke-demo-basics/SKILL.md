---
name: gke-demo-basics
description: >-
  Toy skill for the skill-overlay demo. Covers connecting to a GKE cluster and
  checking node health. Use for basic cluster access questions.
---

# GKE Demo Basics

This is a toy skill. It exists only to demonstrate how a downstream repository
keeps its own changes to a skill while still taking upstream updates.

## Cluster Credentials

Always specify the cluster's region when fetching credentials:

```bash
gcloud container clusters get-credentials CLUSTER --region=REGION --quiet
```

## Checking Node Health

1. List the nodes and confirm each one reports `Ready`.
2. For a node that is not ready, describe it and read its conditions.
3. Check that the node can recieve new pods before you cordon anything.

```bash
kubectl get nodes
kubectl describe node NODE
```

## References

- [CLI reference](references/cli.md)
