---
inclusion: always
---
# Project Structure

```text
vehicle-diagnostic-assistant/
│
├── .kiro/
│   ├── specs/
│   │   └── vehicle-diagnostic-assistant/
│   │       ├── requirements.md
│   │       ├── design.md
│   │       └── tasks.md
│   │
│   └── steering/
│       ├── product.md
│       ├── tech.md
│       └── structure.md
│
├── frontend/
│   └── src/
│       ├── components/
│       ├── pages/
│       ├── services/
│       └── utils/
│
├── backend/
│   ├── src/
│   │   ├── handlers/              # Lambda entry points
│   │   ├── diagnostics/           # AI diagnosis domain
│   │   │   ├── services/
│   │   │   ├── repositories/
│   │   │   ├── models/
│   │   │   ├── prompts/
│   │   │   └── events/
│   │   │
│   │   ├── shared/
│   │   │   ├── auth/
│   │   │   ├── logging/
│   │   │   ├── validation/
│   │   │   └── utils/
│   │   │
│   │   └── tests/
│   │
│   ├── requirements.txt
│   └── template.yaml
│
├── infrastructure/
│   ├── cloudformation/
│   └── environments/
│       ├── dev/
│       ├── qa/
│       └── prod/
│
├── tests/
│   ├── integration/
│   └── e2e/
│
├── docs/
├── README.md
└── .gitignore
```

---

# Conventions

- Group code by domain (`diagnostics`, `audit`, `escalation`)
- Keep prompts separate from business logic
- Keep Lambda handlers thin
- Store shared utilities in `shared/`
- Use repositories for DynamoDB access
- Use CloudFormation for infrastructure
- Keep environments isolated (`dev`, `qa`, `prod`)
- Never hardcode secrets or credentials

---