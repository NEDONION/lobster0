export type Locale = 'zh-CN' | 'en';
export type CapabilityId = 'runtime' | 'channels' | 'safety' | 'memory' | 'automation';
export type WorkflowId = 'approval' | 'external-cli' | 'multi-channel';

export interface CapabilityCopy {
  id: CapabilityId;
  eyebrow: string;
  label: string;
  title: string;
  summary: string;
  facts: readonly string[];
}

export interface WorkflowCopy {
  id: WorkflowId;
  label: string;
  title: string;
  summary: string;
  image?: {
    src: string;
    alt: string;
    width: number;
    height: number;
  };
}

export interface TraceStepCopy {
  event: (typeof siteFacts.traceEvents)[number];
  state: string;
  detail: string;
}

export interface MarketingCopy {
  meta: {
    title: string;
    description: string;
  };
  nav: {
    product: string;
    workbench: string;
    docs: string;
    github: string;
    language: string;
  };
  hero: {
    eyebrow: string;
    title: string;
    lead: string;
    primaryCta: string;
    secondaryCta: string;
    installLabel: string;
    copyLabel: string;
    copiedLabel: string;
    surfaces: readonly {
      name: string;
      role: string;
      note: string;
    }[];
  };
  trace: {
    ariaLabel: string;
    eyebrow: string;
    title: string;
    description: string;
    steps: readonly TraceStepCopy[];
  };
  evidence: {
    disclosure: string;
    labels: {
      core: string;
      surfaces: string;
      tools: string;
      channelCases: string;
      automationCases: string;
    };
  };
  product: {
    eyebrow: string;
    title: string;
    lead: string;
  };
  capabilities: readonly CapabilityCopy[];
  workbench: {
    eyebrow: string;
    title: string;
    lead: string;
  };
  workflows: readonly WorkflowCopy[];
  quickStart: {
    eyebrow: string;
    title: string;
    lead: string;
    docsCta: string;
    githubCta: string;
  };
  footer: {
    statement: string;
    docs: string;
    issues: string;
    source: string;
  };
}

export const siteFacts = {
  requirements: {
    python: '3.12+',
    node: '22.19+',
  },
  counts: {
    surfaces: 4,
    tools: 18,
    channelCases: 33,
    automationCases: 15,
    permissionModes: 4,
  },
  surfaces: ['TUI', 'Feishu', 'Telegram', 'Discord'],
  permissionModes: ['SAFE', 'SMART', 'AUTOPILOT', 'YOLO'],
  traceEvents: [
    'MESSAGE_RECEIVED',
    'AGENT_PLANNING',
    'POLICY_CHECK',
    'APPROVAL',
    'TOOL_EXECUTION',
    'RESULT_DELIVERED',
  ],
  install: [
    'git clone https://github.com/NEDONION/miniclaw.git',
    'cd miniclaw',
    'uv sync --extra dev --extra channels',
    'pnpm --dir tui install',
    'pnpm --dir tui build',
    'cp .env.example .env',
    'uv run miniclaw init',
    'uv run miniclaw doctor',
    'uv run miniclaw',
  ].join('\n'),
  status: {
    automationDefault: false,
    implementationPassIsLivePass: false,
  },
  links: {
    github: 'https://github.com/NEDONION/miniclaw',
    issues: 'https://github.com/NEDONION/miniclaw/issues',
    product:
      'https://github.com/NEDONION/miniclaw/blob/main/docs/product/20260807_%E4%BA%A7%E5%93%81%E9%9C%80%E6%B1%82%E6%96%87%E6%A1%A3.md',
    architecture:
      'https://github.com/NEDONION/miniclaw/blob/main/docs/architecture/20260807_%E7%B3%BB%E7%BB%9F%E6%9E%B6%E6%9E%84.md',
    evaluation: 'https://github.com/NEDONION/miniclaw/tree/main/evals',
  },
} as const;

export const marketingCopy = {
  'zh-CN': {
    meta: {
      title: 'MiniClaw — 你的本地行动助手',
      description:
        '一个本地优先、边界可检查的开源个人 Agent：同一个 Python Core，通过 TUI、飞书、Telegram 与 Discord 安全行动。',
    },
    nav: {
      product: '特性',
      workbench: '演示',
      docs: '文档',
      github: 'GitHub',
      language: 'English',
    },
    hero: {
      eyebrow: '本地优先 · 开源 · 边界可检查',
      title: '你的本地行动助手。',
      lead:
        '从熟悉的入口发出请求。MiniClaw 在你的机器上理解意图、检查边界、请求审批，并把任务安全做完。',
      primaryCta: '5 分钟开始',
      secondaryCta: '查看源码',
      installLabel: '从源码启动',
      copyLabel: '复制命令',
      copiedLabel: '已复制',
      surfaces: [
        { name: 'TUI', role: '本地界面', note: '完整控制' },
        { name: '飞书', role: '工作入口', note: '团队协作' },
        { name: 'Telegram', role: '移动入口', note: '随时触达' },
        { name: 'Discord', role: '社区入口', note: '独立交付' },
      ],
    },
    trace: {
      ariaLabel: 'MiniClaw 运行轨迹',
      eyebrow: '运行轨迹 / 01',
      title: '行动不是黑盒。',
      description: '一次请求穿过真实运行时的六个状态，每一步都有边界、结果与归属。',
      steps: [
        { event: 'MESSAGE_RECEIVED', state: 'accepted', detail: 'owner / tui' },
        { event: 'AGENT_PLANNING', state: 'running', detail: 'intent → tool' },
        { event: 'POLICY_CHECK', state: 'allowed', detail: 'workspace / argv' },
        { event: 'APPROVAL', state: 'waiting', detail: 'exact arguments' },
        { event: 'TOOL_EXECUTION', state: 'running', detail: 'isolated action' },
        { event: 'RESULT_DELIVERED', state: 'succeeded', detail: 'same conversation' },
      ],
    },
    evidence: {
      disclosure:
        '33 条 versioned Channel cases；15 条 versioned Automation cases。Implementation PASS 不等于 Live PASS。',
      labels: {
        core: 'Python Core',
        surfaces: '使用入口',
        tools: '内置 Tool',
        channelCases: 'Channel cases',
        automationCases: 'Automation cases',
      },
    },
    product: {
      eyebrow: '特性 / 02',
      title: '一个 Core，五个可检查的系统面。',
      lead: '像产品一样易读，像开源工程一样具体。切换能力，直接查看结构、边界与验证证据。',
    },
    capabilities: [
      {
        id: 'runtime',
        eyebrow: '01 / 核心循环',
        label: '运行时',
        title: '从消息到结果，是一条受控执行链。',
        summary: '模型理解意图；Policy 决定边界；Approval 绑定参数；Tool 才真正执行。',
        facts: ['六步 Trace 顺序', 'OpenAI-compatible Provider', '结果返回原对话', '结构化执行记录'],
      },
      {
        id: 'channels',
        eyebrow: '02 / 隔离入口',
        label: '多入口',
        title: '四个入口，共享一个 AgentRuntime。',
        summary: 'TUI、飞书、Telegram 与 Discord 共用能力，故障域与交付状态彼此隔离。',
        facts: ['4 个使用入口', '33 条 versioned cases', 'Transport 隔离', 'Delivery 与 queue 隔离'],
      },
      {
        id: 'safety',
        eyebrow: '03 / 动作前检查',
        label: '安全边界',
        title: '限制发生在动作之前。',
        summary: 'Workspace、exact argv、网络与 Secret 都经过统一 Policy，而不是依赖提示词自觉。',
        facts: ['4 档权限模式', '参数绑定审批', 'Workspace 边界', 'SSRF 与 Secret 防护'],
      },
      {
        id: 'memory',
        eyebrow: '04 / 所有者边界',
        label: '记忆',
        title: 'Markdown 是 Truth，SQLite 是 Projection。',
        summary: '长期事实可读、可审查、由 Owner 掌控；结构化索引可以随时重建。',
        facts: ['Owner 边界', 'Markdown Truth', 'SQLite control plane', '同 Owner 才跨渠道共享'],
      },
      {
        id: 'automation',
        eyebrow: '05 / 显式门禁',
        label: '自动化',
        title: '默认关闭，授权之后才自动化。',
        summary: '自动执行先经过明确启用与安全门，再用 versioned cases 验证实现语义。',
        facts: ['默认 disabled', '15 条 versioned cases', '显式授权 gate', 'Implementation ≠ Live'],
      },
    ],
    workbench: {
      eyebrow: '演示 / 03',
      title: '看真实执行，不看概念渲染。',
      lead: '两张仓库内 TUI 截图，加上一张多入口结构图，展示 MiniClaw 今天已经验证的工作方式。',
    },
    workflows: [
      {
        id: 'approval',
        label: 'SAFE 审批',
        title: '高风险动作先展示影响与 exact argv。',
        summary: 'Owner 看到目标与参数后再决定，授权不会转移给另一组参数。',
        image: {
          src: '/images/miniclaw-tui-approval-warp.webp',
          alt: 'MiniClaw TUI 中的 SAFE 模式工具审批界面',
          width: 2784,
          height: 1824,
        },
      },
      {
        id: 'external-cli',
        label: '外部 CLI',
        title: '程序与参数结构化传递。',
        summary: '外部 CLI 执行结果回到原会话，执行顺序和失败位置保持可观察。',
        image: {
          src: '/images/miniclaw-tui-external-cli-warp.webp',
          alt: 'MiniClaw TUI 中执行 exact-argv 外部 CLI 的界面',
          width: 2696,
          height: 1736,
        },
      },
      {
        id: 'multi-channel',
        label: '多入口',
        title: '共享能力，不共享故障。',
        summary: '一个 AgentRuntime 连接四个入口；每个平台保留独立 Transport、Delivery、queue 与运行期状态。',
      },
    ],
    quickStart: {
      eyebrow: '本地启动',
      title: '从本地开始，看见第一条 Trace。',
      lead: 'Python 3.12+；默认 TUI 需要 Node.js 22.19+。服务凭据始终由你保管。',
      docsCta: '阅读安装文档',
      githubCta: '参与贡献',
    },
    footer: {
      statement: '小核心，明确边界，运行在你的机器上。',
      docs: '文档',
      issues: 'Issues',
      source: '源代码',
    },
  },
  en: {
    meta: {
      title: 'MiniClaw — Your local agent, ready to act.',
      description:
        'A local-first open-source personal agent with inspectable boundaries: one Python core across TUI, Feishu, Telegram, and Discord.',
    },
    nav: {
      product: 'Features',
      workbench: 'Demo',
      docs: 'Docs',
      github: 'GitHub',
      language: '简体中文',
    },
    hero: {
      eyebrow: 'LOCAL-FIRST / OPEN SOURCE',
      title: 'Your local agent, ready to act.',
      lead:
        'Ask from a surface you already use. MiniClaw understands intent, checks boundaries, requests approval, and finishes the work on your machine.',
      primaryCta: 'Start in 5 minutes',
      secondaryCta: 'View source',
      installLabel: 'Run from source',
      copyLabel: 'Copy commands',
      copiedLabel: 'Copied',
      surfaces: [
        { name: 'TUI', role: 'Local control', note: 'Full visibility' },
        { name: 'Feishu', role: 'Work surface', note: 'Team delivery' },
        { name: 'Telegram', role: 'Mobile surface', note: 'Always reachable' },
        { name: 'Discord', role: 'Community', note: 'Isolated delivery' },
      ],
    },
    trace: {
      ariaLabel: 'Claw Trace',
      eyebrow: 'CLAW TRACE / 01',
      title: 'Action is not a black box.',
      description: 'A request crosses six real runtime states, each with a boundary, result, and owner.',
      steps: [
        { event: 'MESSAGE_RECEIVED', state: 'accepted', detail: 'owner / tui' },
        { event: 'AGENT_PLANNING', state: 'running', detail: 'intent → tool' },
        { event: 'POLICY_CHECK', state: 'allowed', detail: 'workspace / argv' },
        { event: 'APPROVAL', state: 'waiting', detail: 'exact arguments' },
        { event: 'TOOL_EXECUTION', state: 'running', detail: 'isolated action' },
        { event: 'RESULT_DELIVERED', state: 'succeeded', detail: 'same conversation' },
      ],
    },
    evidence: {
      disclosure:
        '33 versioned Channel cases; 15 versioned Automation cases. Implementation PASS is not Live PASS.',
      labels: {
        core: 'Python core',
        surfaces: 'surfaces',
        tools: 'built-in tools',
        channelCases: 'Channel cases',
        automationCases: 'Automation cases',
      },
    },
    product: {
      eyebrow: 'FEATURES / 02',
      title: 'One core. Five inspectable system views.',
      lead: 'Readable like a product, specific like an open-source system. Switch views to inspect structure, boundaries, and evidence.',
    },
    capabilities: [
      {
        id: 'runtime',
        eyebrow: '01 / CORE LOOP',
        label: 'Runtime',
        title: 'Message to result is a controlled execution path.',
        summary: 'The model reads intent. Policy sets limits. Approval binds arguments. Only then can a Tool act.',
        facts: ['Six-step Trace', 'OpenAI-compatible Provider', 'Results return in context', 'Structured execution records'],
      },
      {
        id: 'channels',
        eyebrow: '02 / ISOLATED EDGES',
        label: 'Channels',
        title: 'Four surfaces share one AgentRuntime.',
        summary: 'TUI, Feishu, Telegram, and Discord share capability while delivery and failure state stay isolated.',
        facts: ['4 user surfaces', '33 versioned cases', 'Isolated transports', 'Isolated delivery and queues'],
      },
      {
        id: 'safety',
        eyebrow: '03 / POLICY BEFORE ACTION',
        label: 'Safety',
        title: 'Limits happen before action.',
        summary: 'Workspace, exact argv, network, and secrets pass through one Policy—not a promise in a prompt.',
        facts: ['4 permission modes', 'Argument-bound approval', 'Workspace boundary', 'SSRF and secret guards'],
      },
      {
        id: 'memory',
        eyebrow: '04 / OWNER BOUNDARY',
        label: 'Memory',
        title: 'Markdown is Truth. SQLite is Projection.',
        summary: 'Durable facts stay readable, reviewable, and owner-controlled; structured indexes can be rebuilt.',
        facts: ['Owner boundary', 'Markdown Truth', 'SQLite control plane', 'Cross-channel only for one Owner'],
      },
      {
        id: 'automation',
        eyebrow: '05 / EXPLICIT GATE',
        label: 'Automation',
        title: 'Disabled by default. Automated after consent.',
        summary: 'Automation passes an explicit enablement and safety gate before versioned cases validate behavior.',
        facts: ['Disabled by default', '15 versioned cases', 'Explicit authorization gate', 'Implementation ≠ Live'],
      },
    ],
    workbench: {
      eyebrow: 'DEMO / 03',
      title: 'Inspect real execution—not a concept render.',
      lead: 'Two repository-owned TUI captures and one channel diagram show the workflows MiniClaw can substantiate today.',
    },
    workflows: [
      {
        id: 'approval',
        label: 'SAFE Approval',
        title: 'Risky actions show impact and exact argv first.',
        summary: 'The Owner decides with the target and parameters visible; approval never transfers to different arguments.',
        image: {
          src: '/images/miniclaw-tui-approval-warp.webp',
          alt: 'Tool approval in the MiniClaw TUI SAFE mode',
          width: 2784,
          height: 1824,
        },
      },
      {
        id: 'external-cli',
        label: 'External CLI',
        title: 'Programs and arguments stay structured.',
        summary: 'External CLI results return to the same conversation with execution order and failures visible.',
        image: {
          src: '/images/miniclaw-tui-external-cli-warp.webp',
          alt: 'Exact-argv external CLI execution in the MiniClaw TUI',
          width: 2696,
          height: 1736,
        },
      },
      {
        id: 'multi-channel',
        label: 'Multi-channel',
        title: 'Share capability, not failure.',
        summary: 'One AgentRuntime connects four surfaces; every platform keeps its own transport, delivery, queue, and runtime state.',
      },
    ],
    quickStart: {
      eyebrow: 'RUN IT YOURSELF',
      title: 'Start local. See your first Trace.',
      lead: 'Python 3.12+; the default TUI needs Node.js 22.19+. You keep every service credential.',
      docsCta: 'Read the install guide',
      githubCta: 'Contribute',
    },
    footer: {
      statement: 'Small core. Explicit boundaries. Your machine.',
      docs: 'Docs',
      issues: 'Issues',
      source: 'Source',
    },
  },
} as const satisfies Record<Locale, MarketingCopy>;
