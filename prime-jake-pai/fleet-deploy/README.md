# RIG Fleet Deployment — Model Endpoints & Installation

## Trained Models

| Model | Endpoint | Port | GPU | Purpose |
|-------|----------|------|-----|---------|
| rig-kat | http://NODE_IP:8003/v1 | 8003 | GPU 1 | RIG-KAT-Coder V3 (fine-tuned with RIG doctrine) |
| rig-kat-v3 | http://NODE_IP:8003/v1 | 8003 | GPU 1 | Base KAT-Coder (no LoRA) |
| blackwell-daily | http://NODE_IP:8001/v1 | 8001 | GPU 0+2 | Daily driver (DeepSeek-V4-Flash) |
| qwen3:8b | http://NODE_IP:11434 | 11434 | CPU | Fast simple queries |
| qwen3:14b | http://NODE_IP:11434 | 11434 | CPU | Medium queries |

## Training Results (10 Rounds)

| Round | Loss | LR |
|-------|------|-----|
| 1 | 0.809 | 1e-4 |
| 2 | 0.849 | 5e-5 |
| 3 | 1.038 | 3e-5 |
| 4 | 1.021 | 2e-5 |
| 5 | 0.891 | 1.5e-5 |
| 6 | 0.982 | 1e-5 |
| 7 | 0.956 | 8e-6 |
| 8 | 0.955 | 6e-6 |
| 9 | 1.008 | 4e-6 |
| 10 | 1.086 | 2e-6 |

Best model: Round 1 (loss 0.809, 50% better than v1's 1.618)
Final model: Round 10 (consolidated through 10 epochs continual learning)

## Installation on Fleet Nodes

### Prerequisites
- Python 3.10+
- NVIDIA GPU with 80GB+ VRAM (for KAT-Coder)
- vLLM 0.25.1+ installed
- KAT-Coder-V2.5-Dev model downloaded

### Step 1: Download LoRA Adapter
```bash
# From QNAP
python3 ~/.rig/scripts/qnap-bridge.py get models/rig-kat-round1.safetensors ~/.rig/adapters/rig-kat/adapter_model.safetensors
python3 ~/.rig/scripts/qnap-bridge.py get models/rig-kat-v3-final.safetensors ~/.rig/adapters/rig-kat-v3/adapter_model.safetensors
```

### Step 2: Download Adapter Config
```bash
mkdir -p ~/.rig/adapters/rig-kat
cat > ~/.rig/adapters/rig-kat/adapter_config.json << 'EOF'
{
  "base_model_name_or_path": "/path/to/KAT-Coder-V2.5-Dev",
  "bias": "none",
  "lora_alpha": 256,
  "lora_dropout": 0.05,
  "lora_r": 128,
  "peft_type": "LORA",
  "target_modules": ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
  "task_type": "CAUSAL_LM"
}
EOF
```

### Step 3: Start vLLM with LoRA
```bash
CUDA_VISIBLE_DEVICES=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
FLASHINFER_DISABLE_VERSION_CHECK=1 \
vllm serve /path/to/KAT-Coder-V2.5-Dev \
  --served-model-name rig-kat \
  --host 0.0.0.0 --port 8003 \
  --enable-lora \
  --max-lora-rank 128 \
  --lora-modules rig-kat=~/.rig/adapters/rig-kat \
  --gpu-memory-utilization 0.90 \
  --language-model-only \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --max-num-seqs 16 \
  --trust-remote-code \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --kv-cache-dtype fp8
```

### Step 4: Test
```bash
curl http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"rig-kat","messages":[{"role":"user","content":"What is Gate-D?"}],"max_tokens":200}'
```

### Step 5: Set Up Model Router (Optional)
```bash
# Install model router for intelligent query routing
python3 ~/.rig/scripts/model_router.py serve --port 8010
```

### Step 6: Set Up Mixture of Agents (Optional)
```bash
# Multi-agent ensemble with verification
python3 ~/.rig/scripts/mixture_of_agents.py serve --port 8020
```

## Fleet Node Assignments

| Node | Role | Model | Port |
|------|------|-------|------|
| blackwell | Coding + GPU inference | rig-kat | 8003 |
| rig-96gb | Synthesis + creative QA | rig-kat | 8003 |
| rig-256gb | Strategy + long-context | blackwell-daily | 8001 |
| rig-36gb | Signal research | qwen3:8b | 11434 |
| rig-128gb-mbp | Verification | qwen3:14b | 11434 |

## 24/7 Services

| Service | Purpose | Interval |
|---------|---------|----------|
| Fleet Orchestrator | Monitor all nodes/services/GPUs | 60s |
| Data Flywheel | Collect/ferment/merge training data | 4hr |
| Prime Agent Watchdog | Keep Prime Agent alive | 30s |

## API URLs

### Blackwell (primary)
- RIG-KAT-Coder: http://192.168.68.90:8003/v1
- Daily driver: http://192.168.68.90:8001/v1
- Ollama: http://192.168.68.90:11434
- Model Router: http://192.168.68.90:8010
- Mixture of Agents: http://192.168.68.90:8020

### Tailscale (remote access)
- RIG-KAT-Coder: http://100.67.126.117:8003/v1
- Daily driver: http://100.67.126.117:8001/v1
