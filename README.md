# Agentic RAG on AWS EKS

A document Q&A agent built with Streamlit, ChromaDB and Gemini function calling.
The agent decides per question whether to search the uploaded documents, do a
calculation, or check the time. The app first ran as a single Docker container on
EC2; this repo moves it to an EKS cluster provisioned with Terraform and deployed
with Argo CD.

## Layout

```
02-eks/                    Terraform: VPC, EKS cluster, Secrets Manager, IAM role
agentic-rag-gemini-v1/     Application, Dockerfile, docker-compose
  k8s/                     Deployment, Service, ExternalSecret
  argocd-app.yaml          Argo CD Application pointing at k8s/
```

Placeholders in angle brackets (`<AWS_ACCOUNT_ID>`, `<YOUR_STATE_BUCKET>`, ...) need
to be replaced with your own values.

## Run locally

```bash
cd agentic-rag-gemini-v1
cp .env.example .env        # fill in the key and password
docker compose up -d --build
```

The app is at http://localhost:8501.

## Deploy to EKS

1. Create an S3 bucket and a DynamoDB table (`terraform-lock`, partition key `LockID`)
   for Terraform state, and set the bucket in `02-eks/backend.tf`. With Terraform 1.10
   or newer you can skip the DynamoDB table (see State locking below).
2. Create the cluster:

   ```bash
   cd 02-eks
   cp terraform.tfvars.example terraform.tfvars
   terraform init
   terraform apply
   $(terraform output -raw configure_kubectl)
   ```

3. Put the app secrets in Secrets Manager (the secret itself is created by Terraform):

   ```bash
   aws secretsmanager put-secret-value --secret-id agentic-rag/app-secrets \
     --secret-string file://secret.json
   ```

   where `secret.json` contains `{"GEMINI_API_KEY": "...", "APP_PASSWORD": "..."}`.
   Do not commit that file.

4. Build and push the image to ECR:

   ```bash
   aws ecr create-repository --repository-name agentic-rag
   aws ecr get-login-password | docker login --username AWS --password-stdin \
     <AWS_ACCOUNT_ID>.dkr.ecr.<AWS_REGION>.amazonaws.com
   docker build -t <AWS_ACCOUNT_ID>.dkr.ecr.<AWS_REGION>.amazonaws.com/agentic-rag:v1 \
     agentic-rag-gemini-v1
   docker push <AWS_ACCOUNT_ID>.dkr.ecr.<AWS_REGION>.amazonaws.com/agentic-rag:v1
   ```

5. Install External Secrets Operator, using the role created by Terraform:

   ```bash
   helm repo add external-secrets https://charts.external-secrets.io
   helm install external-secrets external-secrets/external-secrets \
     -n external-secrets --create-namespace \
     --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$(terraform -chdir=02-eks output -raw external_secrets_role_arn)"
   ```

6. Install Argo CD, then register the application:

   ```bash
   kubectl create namespace argocd
   kubectl apply -n argocd --server-side \
     -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
   kubectl apply -f agentic-rag-gemini-v1/argocd-app.yaml
   ```

7. Open the app:

   ```bash
   kubectl -n agentic-rag port-forward svc/agentic-rag-svc 8501:8501
   ```

To release a change, push a new image tag, update the `image:` in
`k8s/deployment.yaml` and commit. Argo CD rolls it out.

## Teardown

```bash
kubectl delete application agentic-rag -n argocd
cd 02-eks && terraform destroy
```

## State locking

`backend.tf` locks the state with a DynamoDB table. On Terraform 1.10 or newer the
table is not needed: replace `dynamodb_table = "terraform-lock"` with
`use_lockfile = true` in the backend block and run `terraform init -reconfigure`.

```hcl
backend "s3" {
  bucket       = "<YOUR_STATE_BUCKET>"
  key          = "eks/terraform.tfstate"
  region       = "<AWS_REGION>"
  encrypt      = true
  use_lockfile = true
}
```

Terraform then keeps a lock file (`terraform.tfstate.tflock`) next to the state file in
the same bucket. The IAM identity running Terraform needs `s3:PutObject` and
`s3:DeleteObject` on that key in addition to the usual state access. Also raise
`required_version` in `backend.tf` to `>= 1.10`.

## Data persistence

The Chroma index is stored inside the container, so it is lost when the pod restarts.
To keep it, add a PersistentVolumeClaim (this needs the EBS CSI driver add-on on the
cluster), mount it at `/data` in `k8s/deployment.yaml`, and set the Deployment strategy
to `Recreate`, since the volume can only be attached to one pod at a time.
