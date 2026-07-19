# Architecture Diagram

Use this Mermaid source to render the architecture diagram for submission.
Render at: https://mermaid.live/ or include directly in the README.

## System Overview

```mermaid
flowchart TD
    subgraph "Data Sources (Public Disclosures)"
        SEC[SEC Form 4<br/>Insider Buys/Sells]
        BRK[Berkshire 13F<br/>Quarterly Holdings]
        ARK[ARK Invest<br/>Daily ETF Holdings]
        MOAT[MOAT ETF<br/>Wide Moat Holdings]
    end

    subgraph "Rule Engine"
        SM[Smart Money Scanner<br/>17→4 providers in demo]
        CS[Conviction Scorer<br/>Multi-source agreement]
        TF[Trading Filter<br/>Entry paths A/B/Overlap]
    end

    subgraph "Qwen Agent Layer"
        QC[QwenClient<br/>DashScope API]
        CC[Catalyst Classifier<br/>News → sentiment + type]
        SA[Signal Arbitrator<br/>Portfolio-aware ranking]
        CG[Commentary Generator<br/>Natural language explanations]
    end

    subgraph "Safety & Execution"
        RM[Risk Manager<br/>Absolute veto layer]
        CB[Circuit Breakers<br/>DD limits + halt lock]
        BR[Broker<br/>IBKR Paper / Mock]
    end

    subgraph "Dashboard"
        API[FastAPI Server<br/>REST endpoints]
        UI[React Dashboard<br/>Real-time monitoring]
    end

    SEC --> SM
    BRK --> SM
    ARK --> SM
    MOAT --> SM
    SM --> CS
    CS --> TF
    TF -->|"candidates"| SA
    QC --> CC
    QC --> SA
    QC --> CG
    CC -.->|"enhances"| TF
    SA -->|"ranked signals"| RM
    RM --> CB
    CB -->|"approved"| BR
    CG -->|"async"| API
    BR --> API
    API --> UI

    style QC fill:#fff3e0,stroke:#e8a308
    style CC fill:#fff3e0,stroke:#e8a308
    style SA fill:#fff3e0,stroke:#e8a308
    style CG fill:#fff3e0,stroke:#e8a308
    style RM fill:#e8f5e9,stroke:#2e7d32
    style CB fill:#e8f5e9,stroke:#2e7d32
```

## Deployment Architecture

```mermaid
flowchart LR
    subgraph "Alibaba Cloud ECS"
        subgraph "Docker Container :8000"
            FE[React Static Assets]
            API[FastAPI Server]
            TL[Trading Loop<br/>hourly, market-hours gated]
            MB[Mock Broker]
        end
    end

    subgraph "External Services"
        DS[Qwen Cloud<br/>DashScope API]
        YF[yfinance<br/>Market Data]
        EDGAR[SEC EDGAR<br/>Form 4 Filings]
    end

    User[User Browser] -->|"HTTP :8000"| FE
    User -->|"HTTP :8000/api/*"| API
    TL -->|"Qwen reasoning"| DS
    TL -->|"Price data"| YF
    TL -->|"Filings"| EDGAR
    TL -->|"fills"| MB
    TL -->|"state"| API
```

## Trading Cycle Sequence

```mermaid
sequenceDiagram
    participant Loop as Trading Loop (hourly)
    participant Scan as Smart Money Scanner
    participant Qwen as Qwen Cloud
    participant Risk as Risk Manager
    participant Broker as Broker/Mock

    Loop->>Scan: Fetch smart-money filings
    Scan-->>Loop: ranked candidates (conviction scored)

    Loop->>Qwen: Classify catalyst headlines
    Qwen-->>Loop: Enhanced classifications + confidence

    Loop->>Qwen: Rank candidates (portfolio context)
    Qwen-->>Loop: Prioritized action plan + reasoning

    Loop->>Risk: Validate signals (priority order)
    Risk-->>Loop: Approved / Rejected (with reasons)

    Loop->>Broker: Submit bracket orders (approved only)
    Broker-->>Loop: Fill confirmations

    Loop->>Qwen: Generate commentary (async, non-blocking)
    Qwen-->>Loop: Natural language cycle summary
```
