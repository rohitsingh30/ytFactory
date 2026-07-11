# ytFactory — High-Level Design (logical, deployment-agnostic)

The system as a set of responsibilities. Where anything runs (laptop,
cloud, GPU) is an implementation detail deliberately omitted here.

## 1 · System overview — layers

```mermaid
flowchart TB
    Req(["Content Request<br/>channel + topic"])

    subgraph CFG["CONFIGURATION · rules per channel"]
        Channel[Channel definition]
        Variant[Niche variant overlay]
        Channel --> Variant
    end

    subgraph PIPE["CONTENT PIPELINE · idea to video"]
        direction TB
        P1["1 · Source<br/>fetch real material"]
        P2["2 · Script<br/>LLM edits into narration"]
        P3["3 · Cast<br/>lock character look"]
        P4["4 · Visuals<br/>generate panels"]
        P5["5 · Narration<br/>synthesize voice"]
        P6["6 · Align<br/>word-level timing"]
        P7["7 · Assemble<br/>mux to video"]
        P8["8 · Publish"]
        P1 --> P2 --> P3 --> P4 --> P5 --> P6 --> P7 --> P8
    end

    subgraph ENGINE["RENDER ENGINE · how visuals+audio become a video"]
        Spec["RenderSpec (one source of truth)"]
        Slots["Pluggable capabilities<br/>audio · visuals · timeline · overlays · music · compose"]
        Spec --> Slots
    end

    Video(["Published Video"])

    Req --> CFG
    CFG -->|resolves into| Spec
    CFG --> PIPE
    PIPE -->|steps 4-7 driven by| ENGINE
    P8 --> Video

    classDef cfg fill:#4c1d95,stroke:#a78bfa,color:#fff
    classDef pipe fill:#14532d,stroke:#4ade80,color:#fff
    classDef eng fill:#7c2d12,stroke:#fb923c,color:#fff
    classDef io fill:#0c4a6e,stroke:#38bdf8,color:#fff
    class CFG,Channel,Variant cfg
    class PIPE,P1,P2,P3,P4,P5,P6,P7,P8 pipe
    class ENGINE,Spec,Slots eng
    class Req,Video io
```

## 2 · Design principles (the alignment target)

```mermaid
flowchart LR
    A["Config-driven<br/>channels are data, not code"]
    B["LLM edits real sources<br/>never invents from nothing"]
    C["One immutable spec<br/>describes a whole render"]
    D["Capabilities are plugins<br/>add = 1 module, no core edits"]
    E["Fail loud<br/>no placeholder outputs"]

    classDef p fill:#1e3a8a,stroke:#60a5fa,color:#fff
    class A,B,C,D,E p
```

## 3 · Render engine — the extensibility core

```mermaid
flowchart LR
    Spec["RenderSpec<br/>(built once, immutable)"]
    Select{"select by spec"}
    Reg[("Capability Registry")]

    Spec --> Select --> Reg

    subgraph Caps["Pluggable capability slots"]
        direction TB
        V[visuals]
        A[audio]
        T[timeline]
        O["overlays (many)"]
        M[music]
        C[compose]
    end
    Reg --> Caps

    New["New style / voice / layout<br/>= drop in one module"] -.registers.-> Reg

    classDef core fill:#7c2d12,stroke:#fb923c,color:#fff
    classDef slot fill:#164e63,stroke:#22d3ee,color:#fff
    class Spec,Select,Reg core
    class Caps,V,A,T,O,M,C slot
```
