# PRD — Cláudio v2

**Status:** Aprovado  
**Versão:** 0.1  
**Data:** 2026-06-14  
**Autor:** Conrado Nogueira + Claude  

---

## 1. Visão

Cláudio é o assistente pessoal do Conrado — a interface conversacional de toda a plataforma AgentForge. Ele não é apenas um chatbot com memória. Ele é um orquestrador: recebe uma intenção em linguagem natural e mobiliza todo o ecossistema de agentes, skills e ferramentas do AgentForge para executá-la.

**Antes:** "Cláudio, cria um vídeo sobre benchmark de modelos."  
Cláudio respondia: "Posso te ajudar com isso." (e eventualmente tentava fazer com uma tool genérica)

**Depois:** Cláudio entende a intenção, ativa o agente `seletor`, depois `roteirista`, depois `animador`, acompanha o progresso e devolve o resultado via Telegram com status em tempo real.

Cláudio v2 é o Cláudio redesenhado do zero — mesmo nome, mesma identidade, arquitetura completamente nova, impulsionado pelo AgentForge.

---

## 2. Problema que estamos resolvendo

O Cláudio atual (fork do Aurelia/Pi SDK) tem um problema arquitetural: foi projetado para modelos frontier (Claude Sonnet/Opus) e não para modelos locais. O resultado:

- **10-15K tokens de system prompt** injetados em todo request — antes do usuário dizer uma palavra
- **Memória de até 40K chars** injetada sempre, sem filtro de relevância
- **3 camadas de bridge** entre o pedido e o modelo (Go → TS → Pi SDK → Ollama)
- **7-10 tools sempre ativas**, mesmo para "bom dia"
- **Dream de memória projetado para Claude**, não para qwen local

O qwen3.5:27b — nosso modelo de produção — chega ao limite com 28K tokens livres de um contexto de 48K. Ele pensa com um terço da capacidade.

Cláudio v2 resolve isso pela raiz: arquitetura construída para modelos locais desde o primeiro dia.

---

## 3. Usuário

**Usuário primário:** Conrado Nogueira  
- Desenvolvedor e pesquisador em IA  
- Acessa via Telegram (mobile + desktop) como canal principal  
- Usa Cláudio para: tarefas técnicas no fox-server, orquestração de projetos, memória contínua entre sessões, automação de workflows (canal, infra, pesquisa)  
- Espera que Cláudio conheça o contexto dos projetos ativos, a infra, o histórico relevante — sem precisar reexplicar a cada sessão

**Usuário secundário (futuro):** outros usuários autorizados via whitelist  
**Sistema:** outros agentes que consomem Cláudio via MCP client ou HTTP API

---

## 4. Princípios de Design

Estes princípios são não-negociáveis. Todo trade-off deve ser julgado contra eles.

### P1 — Gestão de contexto por modelo
O fox-server tem 24GB de VRAM (2× RTX 3060 12GB). O contexto disponível depende do modelo em uso:

- **qwen3.5:9b** — ocupa ~9GB de VRAM, restam ~15GB para contexto. Usado apenas como fallback de classificação quando heurística é insuficiente.
- **qwen3.5:27b** — ocupa ~18GB de VRAM, restam ~6GB para contexto. É o executor principal de tudo. Aqui o contexto é escasso e precisa de gestão cuidadosa.

Troca frequente entre modelos causa swap de VRAM (5-15s por troca) — inaceitável para conversação fluida. O design minimiza trocas: 27b é o modelo padrão, sempre carregado.

### P2 — Memória por recuperação, não por injeção
Memória não entra no prompt automaticamente. Entra quando é relevante para o pedido atual. A relevância é determinada por busca semântica, não por tempo de escrita.

### P3 — Intenção antes de execução
Todo request passa por um classificador de intenção antes de qualquer LLM call pesado. O classificador decide: quais tools ativar, quanta memória recuperar, qual agente especializado acionar.

### P4 — Zero bridge
Python direto ao Ollama via AgentForge. Nenhuma camada intermediária opaca entre o system prompt montado e o modelo que o recebe.

### P5 — AgentForge como plataforma
Cláudio não reimplementa capacidades que o AgentForge já tem. Ele é um canal + orquestrador em cima do AgentForge. Skills, tools, agentes especializados — tudo vive no AgentForge e Cláudio os ativa.

### P6 — On-prem first
Toda execução de tarefa usa recursos locais do fox-server por padrão. Ferramentas externas (HeyGen, APIs pagas) só são acionadas quando localmente inviável, e com registro de custo.

### P7 — Mesma identidade
Cláudio v2 é o mesmo Cláudio. Mesma persona, mesmo histórico de memória (migrado), mesma presença no Telegram do Conrado. A reescrita é invisível para o usuário.

### P8 — Gestão ativa de modelos no Ollama
O Ollama não gerencia VRAM de forma segura para este stack. Os modelos não podem coexistir:

- **9b carregado + 27b solicitado** → 27b vaza para CPU/RAM
- **27b carregado + 9b solicitado** → 9b vaza para CPU/RAM

Em ambos os casos: latência de dezenas de segundos, inaceitável.

Regra única: **apenas um modelo por vez na VRAM**. O Cláudio emite `ollama stop <modelo>` explícito antes de qualquer transição, e rastreia internamente qual modelo está atualmente carregado.

Regra de ouro para seleção de modelo: **use o que já está carregado**.

- Se o 27b está na VRAM → usa o 27b para tudo (sessão, classificação, extração de memória)
- Se o 9b está na VRAM → usa o 9b para tarefas leves antes de carregar o 27b
- Só faz swap quando estritamente necessário — o overhead (unload + load + warmup) custa 20-40s, mais do que o ganho de usar o modelo "certo"

Ciclo de vida padrão (27b-only):
1. Sessão inicia → carrega 27b (se não estiver já)
2. Durante sessão → 27b executa tudo: resposta, classificação de fallback, contexto
3. Sessão encerra → extração de memória em background com o modelo que estiver carregado (preferencialmente 27b sem swap)
4. Idle prolongado → pode descarregar para economizar VRAM

O uso do 9b é uma otimização futura, não um requisito da v2.0. Benchmarkar antes de adotar.

Estado de modelo é responsabilidade do Cláudio, não do Ollama.

---

## 5. Capacidades Core

### 5.1 Conversação com memória semântica

Cláudio mantém memória contínua entre sessões. A diferença do v1: a memória é recuperada por relevância semântica (mem0), não injetada em massa.

- Lembra de projetos, decisões, preferências, histórico técnico
- Recupera até 5 fragmentos relevantes por request (alvo: < 500 tokens)
- Consolida nova memória em background após cada sessão (extração via qwen3.5:9b)
- Suporta comando explícito: "lembra que X", "esquece Y"

### 5.2 Orquestração de agentes AgentForge

Cláudio pode delegar tarefas complexas a agentes especializados do AgentForge. O usuário não precisa conhecer os agentes — Cláudio escolhe e orquestra.

Exemplos de delegação:
- "Cria um vídeo sobre o benchmark de modelos desta semana" → seletor → roteirista → prep-tts → voz → animador → montador
- "Faz um relatório do estado do fox-server" → fox-health → infra-specialist
- "Pesquisa os últimos papers sobre MoE em 2026" → agente de pesquisa → Feynman

Cláudio acompanha o progresso e reporta via Telegram com atualizações incrementais.

### 5.3 Execução direta de ferramentas

Para tarefas simples, Cláudio executa diretamente via ferramentas do AgentForge sem delegar a sub-agentes:

- Shell commands no fox-server (com perfis de segurança)
- Leitura e escrita de arquivos
- HTTP requests
- Status de containers Docker
- Consulta ao agent-mesh
- Envio de notificações

### 5.4 Agendamento (Cron)

Cláudio pode agendar tarefas recorrentes ou únicas em linguagem natural:
- "Me avisa todo dia às 8h o status do fox-server"
- "Daqui a 2 horas verifica se o benchmark terminou"

Persiste em SQLite. Sobrevive a restart do serviço.

### 5.5 Contexto de projeto

Cláudio conhece os projetos ativos e pode trabalhar dentro do contexto de um projeto específico:
- Lê CLAUDE.md do projeto
- Conhece o stack, as decisões arquiteturais, o estado atual
- Associa conversas a projetos explicitamente (`/projeto canal`)

---

## 6. Canais

### 6.1 Telegram (primário)
- Bot Telegram com whitelist de usuários
- Suporte a texto, voz (STT via Groq), imagens
- Typing indicator, reactions, formatação markdown
- Suporte a grupos/tópicos (thread ID)
- Comandos: `/projeto`, `/memoria`, `/agenda`, `/status`, `/agentes`

### 6.2 HTTP Chat API
- REST API compatível com o formato atual (:18790 ou nova porta)
- Permite integração com n8n, scripts locais, outros agentes
- Autenticação via token
- Streaming de resposta (SSE)

### 6.3 MCP Server
- Expõe Cláudio como ferramenta MCP consumível pelo Claude Code e outros clientes
- Tool: `ask_claudio(prompt, context?)` → resposta
- Tool: `run_agent(agent_id, input)` → delega ao AgentForge e retorna resultado

### 6.4 MCP Client
- Cláudio pode consumir MCPs externos configurados
- Kuzu research graph, HeyGen, ferramentas futuras
- Configuração via arquivo, não hardcoded

---

## 7. Arquitetura — Visão de Alto Nível

```
┌─────────────────────────────────────────────────┐
│                   CANAIS                        │
│  Telegram │ HTTP API │ MCP Server │ MCP Client  │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│           INTENT CLASSIFIER                     │
│                                                 │
│  1ª camada: heurística (<1ms, zero LLM)         │
│    padrões, palavras-chave, tamanho             │
│    resolve ~80% dos casos                       │
│                                                 │
│  2ª camada: qwen3.5:9b (só se ambíguo)          │
│    fallback raro, aceita swap ocasional         │
│                                                 │
│  Saída: intent + tools necessárias (1-3)        │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│            CONTEXT BUILDER                      │
│  Identidade fixa (~100 tokens)                  │
│  + Memória recuperada via mem0 (~300 tokens)    │
│  + Instruções de intent (~100 tokens)           │
│  + Tools selecionadas (1-3)                     │
│  ─────────────────────────────────────          │
│  Total alvo: < 600 tokens de system prompt      │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│        AgentForge Runtime — qwen3.5:27b         │
│        sempre carregado, executor único         │
│                                                 │
│  Resposta direta  │  Delegação a sub-agente     │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│           BACKGROUND (assíncrono)               │
│  Extração de memória (modelo carregado)          │
│  Consolidação no mem0                           │
│  Atualização do Kuzu graph                      │
│  Runlog / audit                                 │
└─────────────────────────────────────────────────┘
```

**Nota sobre swap de modelo:** o qwen3.5:9b aparece em dois lugares — classifier de fallback (raro, durante a sessão) e extração de memória (background, pós-sessão). Nunca concorrem com o 27b em tempo real. O 27b permanece carregado na VRAM durante toda a sessão ativa.

**Nota sobre gestão de VRAM (ver P8):** 9b e 27b não podem coexistir na VRAM — o que não couber vaza para CPU/RAM, em qualquer direção. O Cláudio mantém estado do modelo carregado e emite `ollama stop` explícito antes de toda transição. Os dois modelos se alternam, nunca coexistem.

---

## 8. Modelo de Memória

### Camadas

Usa a stack existente do fox-server — sem nova tecnologia:

| Camada | Tecnologia | O que armazena | TTL |
|---|---|---|---|
| Curto prazo | SQLite / agent_mesh (`~/.agent-mesh/state.db`) | Turns da sessão, audit log de ações | Sessão + histórico imutável |
| Médio prazo | mem0 (vetores) | Fatos, preferências, decisões, fragmentos relevantes | Permanente |
| Longo prazo | Kuzu graph (`~/.agent-mesh/research-graph.db`) | Relações entre entidades, projetos, tecnologias, decisões arquiteturais | Permanente |
| Contexto de projeto | `.mds` / `CLAUDE.md` local do projeto | Stack, arquitetura, estado atual | Versionado com o projeto |

### Fluxo de memória

```
Pedido do usuário
  → embed query
  → busca mem0 (top 5 fragmentos relevantes)
  → busca Kuzu (entidades relacionadas)
  → monta contexto de memória (<500 tokens)
  → entrega ao Context Builder

Após resposta
  → modelo atualmente carregado extrai: fatos novos, correções, decisões
  → armazena no mem0
  → atualiza Kuzu se relevante
  (preferencialmente 27b sem swap; 9b só se já estiver na VRAM)
```

### Migração do Aurelia

Os arquivos `.md` de memória do `~/.aurelia/memory/` são importados uma vez no mem0 como seed. O Cláudio v2 não lê arquivos de memória em disco — tudo via mem0 após a migração.

---

## 9. Modelo de Segurança

**Princípio:** o canal não define o perfil — a operação define. Usuário único não significa confiança irrestrita. Cláudio nunca confia cegamente, independente de quem pediu ou por onde chegou.

### Perfis por operação

| Perfil | Capacidade | Ativação |
|---|---|---|
| `chat` | Só responde, sem tools | Padrão para conversação |
| `read` | Lê arquivos e status, sem escrita | Consultas explícitas |
| `execute` | Shell no escopo definido, sem comandos destrutivos | Tarefas técnicas autorizadas |
| `privileged` | Acesso amplo | Requer confirmação explícita a cada uso |

O perfil `privileged` não é o padrão do Telegram só porque é o Conrado. Cada operação começa no perfil mínimo necessário e escala sob demanda.

### Regras fixas (invioláveis, independente de perfil ou canal)

- **Ações destrutivas sempre pedem confirmação:** `rm`, stop de serviço, alteração de rede, drop de banco, sobrescrever arquivo importante
- **Nunca acessar secrets diretamente:** `.env`, chaves, tokens, credenciais — mesmo que o usuário peça; orientar o usuário a executar ele mesmo se necessário
- **Nunca exfiltrar dados** via curl/wget/http para destinos não reconhecidos
- **Comandos com efeitos colaterais amplos** (`killall`, `docker rm -f`, `iptables`) requerem confirmação mesmo em `privileged`
- **Sem execução silenciosa:** toda tool call que modifica estado do sistema é logada e, quando relevante, reportada ao usuário

### Auditoria

Toda ação de perfil `execute` ou `privileged` grava entrada no runlog com: timestamp, operação, parâmetros, resultado. Não negociável.

---

## 10. Critérios de Sucesso

### Qualidade de raciocínio
- qwen3.5:27b com < 600 tokens de system prompt vs > 10K tokens no Aurelia
- Alvo: 0 falhas de tool calling por excesso de tools (problema documentado: > 5 tools degrada qwen)
- Alvo: tempo de resposta para conversação simples < 15s (vs > 30s no Aurelia hoje)
- Respostas com tool call: < 60s aceitável

### Memória
- Recuperação semântica retorna fragmentos relevantes em ≥ 80% dos casos (avaliação manual, 20 queries)
- Consolidação de memória pós-sessão: < 120s (background, usuário não espera)

### Orquestração
- Cláudio consegue orquestrar pipeline completo de vídeo (7 agentes) a partir de um pedido em linguagem natural
- Status de progresso entregue ao usuário a cada etapa concluída
- Timeout por agente individual: 900s (alinhado com OLLAMA_TIMEOUT do AgentForge)

### Disponibilidade
- Serviço systemd, restart automático
- Cold start do serviço (sem modelo): < 10s
- First inference após carregar modelo (warmup): < 30s aceitável

---

## 11. Fora de Escopo (v2.0)

Explicitamente **não** fazemos na primeira versão:

- Multi-usuário com perfis distintos (suporte técnico para outros usuários além do Conrado)
- Fine-tuning do qwen para a persona do Cláudio
- Síntese de voz do Cláudio (TTS com personalidade) via Telegram
- Interface web/dashboard
- Suporte a WhatsApp (aguarda decisão de CNPJ + Evolution API)
- Replicação/backup distribuído da memória

---

## 12. Decisões Tomadas

Questões resolvidas antes da implementação:

| Questão | Decisão |
|---|---|
| Porta do HTTP API | **Manter :18790** — compatibilidade com n8n e integrações existentes |
| Migração de memória | **Automática no first-boot** — perda pontual de contexto é aceitável |
| Identidade visual no Telegram | **Sem avatar por enquanto** — adicionar quando houver identidade visual definida |
| Persistência do Cron | **Por tipo:** alarme único = executa e descarta; recorrente ("todo dia às 8h") = persiste em SQLite |
| Stack de memória | **Usar stack existente:** SQLite/agent_mesh (curto prazo + audit), mem0 (semântica), Kuzu (grafo), .mds de projeto (contexto local) |
| 9b vs 27b-only | **Começar com 27b para tudo** — se swap mostrar ganho real em benchmark pós-v2.0, reconsiderar |

---

## 13. Logs e Observabilidade

### Princípio

Cláudio executa ações com efeitos reais no servidor. Toda ação com efeito colateral deve ser **auditável** — não apenas logada. A diferença é fundamental:

- **Log:** registro operacional, pode rotacionar, serve para debugging imediato
- **Audit trail:** registro permanente, append-only, imutável — serve para reconstruir o que aconteceu, quando, por que, e com qual resultado

O audit trail do Cláudio deve permitir responder, semanas depois: _"o que o Cláudio fez às 3h da manhã de quinta?"_ com resposta completa e verificável.

### O que registrar e onde

| Evento | Nível | runlog | audit_log |
|---|---|---|---|
| Sessão iniciada / encerrada | INFO | ✓ | ✓ |
| Mensagem recebida (canal, tamanho, sem conteúdo) | INFO | ✓ | — |
| Intent classificado | DEBUG | ✓ | — |
| Tool call executada (nome, parâmetros, resultado) | INFO | ✓ | ✓ |
| Agente AgentForge acionado (id, input resumido) | INFO | ✓ | ✓ |
| Modelo carregado / descarregado | INFO | ✓ | — |
| Ação destrutiva — confirmada ou recusada | WARN | ✓ | ✓ |
| Erro em tool call | ERROR | ✓ | ✓ |
| Extração de memória concluída | DEBUG | ✓ | — |

**Regra:** tudo que modifica estado do sistema vai para o `audit_log`, sem exceção.

### Destinos

| Destino | Tipo | Retenção | Propósito |
|---|---|---|---|
| `~/.claudio/logs/claudio.log` | Log rotativo | 30 dias | Debugging operacional, fox-noc |
| `~/.agent-mesh/state.db` → `audit_log` | Append-only, imutável | Permanente | Audit trail compartilhado com o ecossistema |
| `journalctl -u claudio` | systemd journal | 7 dias | Erros críticos, falhas de startup |

**O `audit_log` do agent_mesh é o registro canônico.** É append-only por design (apenas INSERT, nunca UPDATE/DELETE), compartilhado com Aurelia/Gemini/Agy, e consultável por qualquer agente do ecossistema.

### Formato

Linha única por evento, parseável:
```
2026-06-14T03:12:45Z [INFO] tool=run_bash cmd="docker ps" duration=1.2s
2026-06-14T03:12:46Z [INFO] agent=roteirista status=started input_tokens=312
2026-06-14T03:13:10Z [WARN] action=destructive tool=run_bash cmd="docker stop n8n" confirmed=true
```

### Rotação e retenção

- Rotação diária, retenção de 30 dias
- Sem PII nos logs (conteúdo das mensagens não é logado, apenas metadados)

### Integração com fox-noc

O fox-noc já tem aba de servidor em tempo real. O runlog do Cláudio deve ser acessível via endpoint do fox-noc ou leitura direta — a definir na spec técnica.

---

## 14. Próximos Passos

1. **Aprovação deste PRD** — revisar, ajustar, validar escopo
2. **Arqueologia adicional** — mapear os canais (MCP server/client, chatapi) do Aurelia para entender o que precisa ser reescrito vs portado
3. **Arquitetura técnica** — blueprint detalhado de componentes, interfaces, fluxo de dados
4. **Spec técnica** — contratos entre componentes, modelo de dados, protocolo de memória
5. **Plano de implementação** — fases, ordem, o que valida o quê
6. **Implementação — Fase 1** — core: intent classifier + context builder + AgentForge runtime + Telegram + logs

---

*Este PRD é o documento fundacional. Qualquer decisão de implementação que contradiga um princípio de design (seção 4) deve ser escalada antes de seguir.*
