---
inclusion: always
---

# tech.md

# Technology Stack & Engineering Standards

This project builds an AI-powered Vehicle Diagnostic Assistant using a serverless, event-driven AWS architecture.

The goal is to ensure:
- scalable architecture
- secure AI integration
- maintainable code generation
- consistent implementation patterns
- production-grade observability
- enterprise governance

---

# Core Architecture Principles

## 1. Serverless First

Always prefer managed serverless AWS services.

### Approved Services
- AWS Lambda
- Amazon API Gateway
- Amazon DynamoDB
- AWS Step Functions
- Amazon EventBridge
- Amazon SQS
- Amazon SNS
- Amazon Bedrock
- Amazon S3

### Do Not Use
- EC2
- ECS
- Kubernetes
- Self-managed infrastructure

---

## 2. API First

Every business capability must be exposed through REST APIs.

### Requirements
- OpenAPI specification required
- JSON request/response format
- Stateless APIs only
- JWT authentication mandatory

### API Naming Convention

Good:
- `/diagnostics/analyze`
- `/diagnostics/history`
- `/repair-estimates`

Avoid:
- `/getData`
- `/runProcess`
- `/vehicleStuff`

---

## 3. Event-Driven Design

Services communicate asynchronously whenever possible.

### Preferred Communication
- EventBridge
- SQS
- SNS

### Avoid
- Lambda-to-Lambda direct synchronous calls
- Shared database coupling

---

## 4. AI Governance by Design

Every AI workflow must include:

- confidence scoring
- Bedrock Guardrails
- audit logging
- input validation
- escalation handling

AI features are incomplete without these controls.

---

# Approved Technology Stack

| Layer | Technology |
|---|---|
| Frontend | React |
| API Layer | Amazon API Gateway |
| Authentication | Amazon Cognito |
| Compute | AWS Lambda |
| AI Platform | Amazon Bedrock |
| AI Agents | Amazon Bedrock AgentCore |
| Database | Amazon DynamoDB |
| Object Storage | Amazon S3 |
| Event Bus | Amazon EventBridge |
| Queue | Amazon SQS |
| Notifications | Amazon SNS |
| Workflow | AWS Step Functions |
| Monitoring | Amazon CloudWatch |
| Tracing | AWS X-Ray |
| Secrets | AWS Secrets Manager |
| IaC | AWS CloudFormation |

---

# Backend Standards

## Language Requirements

Backend development must use:
- Python 3.12+
- Boto3 SDK

Do not use:
- Node.js
- Java
- Go

Unless explicitly approved.

---

## Lambda Standards

All Lambda functions must:

- remain stateless
- use structured JSON logging
- implement retry handling
- define explicit timeouts
- include correlation IDs

### Required Log Format

```json
{
  "correlationId": "uuid",
  "service": "diagnostic-service",
  "level": "INFO",
  "message": "Vehicle diagnosis completed"
}
```

---

# Frontend Standards

## Frontend Framework

Use:
- React

---


# AI Engineering Standards

## Bedrock Usage

All AI model calls must use Amazon Bedrock.

### Approved Models
- Nova
- Llama

Direct external model APIs are prohibited.

---

## Prompt Engineering Rules

### Always
- use reusable prompt templates
- sanitize user input
- validate prompt payloads

### Never
- concatenate raw user input directly into prompts
- expose internal prompts to users

---

## AI Response Requirements

Every AI response must include:

```json
{
  "probableIssue": "Weak Battery",
  "repairRecommendation": "Replace battery",
  "estimatedCost": "₹4,000 - ₹8,000",
  "confidenceScore": 0.89
}
```

---

## Confidence Threshold

Escalate responses when:
- confidenceScore < 0.70
- unsafe recommendations detected
- diagnosis is ambiguous

---

## Guardrails

Apply Bedrock Guardrails on every AI endpoint.

Required protections:
- harmful content filtering
- prompt injection prevention
- PII detection
- unsafe repair guidance blocking

---

# Database Standards

## DynamoDB Design

Use:
- single-table design where practical
- partition-key optimized access
- TTL for temporary data

---

## Required Tables

### Diagnostic Sessions
Stores:
- symptoms
- diagnosis
- confidence score
- repair recommendation
- timestamps


---

# Security Standards

## Authentication

Use:
- Amazon Cognito
- OAuth2/JWT

Custom authentication systems are prohibited.

---

## Authorization

Apply:
- least-privilege IAM
- RBAC controls

---

## Encryption

Enable:
- TLS for APIs
- DynamoDB SSE
- S3 SSE

---

## Secrets Management

Use AWS Secrets Manager.

Never:
- hardcode credentials
- store secrets in code
- commit `.env` files

---

# Observability Standards

## Logging

Every service must emit:
- structured JSON logs
- correlation IDs
- request IDs

---

## Monitoring

Create CloudWatch alarms for:
- Lambda error rate
- API latency
- Bedrock response latency
- token usage spikes
- confidence score degradation

---


# Reliability Standards

## Retry Policy

Use:
- exponential backoff
- jitter strategy

---

## DLQ Requirements

Every:
- async Lambda
- SQS consumer

Must have:
- dead-letter queue configured

---

## Circuit Breakers

Implement circuit breakers for:
- AI model dependencies
- downstream APIs
- external integrations

---

# Infrastructure Standards

## Infrastructure as Code

All infrastructure must use:
- AWS CloudFormation

Avoid:
- manual console provisioning

---

## Environment Structure

Required environments:
- dev
- qa
- prod

Environment isolation is mandatory.

---

# Testing Standards


## Required Test Types

### Unit Tests
- Lambda handlers
- services
- repositories

### Integration Tests
- API Gateway integration
- Bedrock integration
- DynamoDB integration

### End-to-End Tests
- full vehicle diagnosis workflow
- escalation workflow

---

# Deployment Standards

## Deployment Strategy

Use:
- blue/green deployment
OR
- canary deployment

---

## Automatic Rollback

Rollback automatically on:
- failed health checks
- elevated error rate
- critical alarms

---

# Anti-Patterns

Avoid the following:

- direct Bedrock calls without guardrails
- synchronous service chaining
- hardcoded secrets
- shared mutable state
- console.log debugging
- monolithic Lambda functions
- oversized prompt payloads
- business logic inside UI components

---