export type Locale = 'zh-CN' | 'en';
export type CapabilityId = 'runtime' | 'channels' | 'safety' | 'memory' | 'automation';
export type WorkflowId = 'denied' | 'crash' | 'isolation';

export interface CapabilityCopy {
  id: CapabilityId;
  eyebrow: string;
  label: string;
  title: string;
  summary: string;
  facts: readonly string[];
}

export type FlowIcon = 'intent' | 'argv' | 'gate' | 'run' | 'result' | 'program' | 'stop';
export type FlowState = 'default' | 'waiting' | 'active' | 'done' | 'blocked';

export interface FlowStepCopy {
  icon: FlowIcon;
  label: string;
  detail: string;
  state: FlowState;
}

export interface WorkflowCopy {
  id: WorkflowId;
  label: string;
  title: string;
  summary: string;
  flow?: readonly FlowStepCopy[];
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
      permissionModes: string;
      dataLocation: string;
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
    'git clone https://github.com/NEDONION/lobster0.git',
    'cd lobster0',
    'uv sync --extra dev --extra channels',
    'pnpm --dir tui install',
    'pnpm --dir tui build',
    'cp .env.example .env',
    'uv run lobster0 init',
    'uv run lobster0 doctor',
    'uv run lobster0',
  ].join('\n'),
  status: {
    automationDefault: false,
    implementationPassIsLivePass: false,
  },
  siteUrl: 'https://lobster0.jchu.tech',
  links: {
    github: 'https://github.com/NEDONION/lobster0',
    issues: 'https://github.com/NEDONION/lobster0/issues',
    product:
      'https://github.com/NEDONION/lobster0/blob/main/docs/product/20260807_%E4%BA%A7%E5%93%81%E9%9C%80%E6%B1%82%E6%96%87%E6%A1%A3.md',
    architecture:
      'https://github.com/NEDONION/lobster0/blob/main/docs/architecture/20260807_%E7%B3%BB%E7%BB%9F%E6%9E%B6%E6%9E%84.md',
    evaluation: 'https://github.com/NEDONION/lobster0/tree/main/evals',
  },
} as const;

export const marketingCopy = {
  'zh-CN': {
    meta: {
      title: 'Lobster0 — 你的本地行动助手',
      description:
        '一个本地优先、边界可检查的开源个人 Agent：同一个 Python Core，通过 TUI、飞书、Telegram 与 Discord 安全行动。',
    },
    nav: {
      product: '特性',
      workbench: '运行机制',
      docs: '文档',
      github: 'GitHub',
      language: '切换语言',
    },
    hero: {
      eyebrow: '本地优先 · 开源 · 边界可检查',
      title: '你的本地行动助手。',
      lead:
        '从熟悉的入口发出请求。Lobster0 在你的机器上理解意图、检查边界、请求审批，并把任务安全做完。',
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
      ariaLabel: 'Lobster0 运行轨迹',
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
        permissionModes: '权限档位',
        dataLocation: '数据存放',
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
      eyebrow: '运行机制 / 03',
      title: '出问题的时候，它怎么做。',
      lead: '被拒绝、执行失败、入口断线——Agent 真正的可信度不在顺利的时候，而在这三种时刻。',
    },
    workflows: [
      {
        id: 'denied',
        label: '审批被拒',
        title: '你说不，它就真的停下。',
        summary: '拒绝不是"换个说法再试"。被拒的那组参数当场作废，Agent 不会绕道、不会重试、不会拆成小步偷偷执行。',
        flow: [
          { icon: 'intent', label: '请求', detail: '"把 /tmp 清干净"', state: 'default' },
          { icon: 'argv', label: '参数锁定', detail: 'rm -rf /tmp/*', state: 'default' },
          { icon: 'gate', label: 'Owner 拒绝', detail: '点了「不允许」', state: 'blocked' },
          { icon: 'stop', label: '当场终止', detail: '这组 argv 直接作废', state: 'blocked' },
        ],
      },
      {
        id: 'crash',
        label: '执行失败',
        title: '失败停在原地，不装作成功。',
        summary: '工具挂了就是挂了。退出码、stderr、失败在第几步，都原样回到对话里，而不是被模型润色成一句"已完成"。',
        flow: [
          { icon: 'program', label: '程序', detail: 'git push origin main', state: 'default' },
          { icon: 'run', label: '子进程退出', detail: 'exit code 128', state: 'blocked' },
          { icon: 'argv', label: '原始 stderr', detail: 'rejected: non-fast-forward', state: 'default' },
          { icon: 'result', label: '如实回报', detail: '失败位置与原因都在', state: 'done' },
        ],
      },
      {
        id: 'isolation',
        label: '故障隔离',
        title: '一个入口挂了，其他照常。',
        summary: '飞书网关断线时，TUI、Telegram、Discord 的队列和交付状态完全不受影响——共享的是能力，不是故障域。',
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
      title: 'Lobster0 — Your local agent, ready to act.',
      description:
        'A local-first open-source personal agent with inspectable boundaries: one Python core across TUI, Feishu, Telegram, and Discord.',
    },
    nav: {
      product: 'Features',
      workbench: 'How it runs',
      docs: 'Docs',
      github: 'GitHub',
      language: 'Switch language',
    },
    hero: {
      eyebrow: 'LOCAL-FIRST / OPEN SOURCE',
      title: 'Your local agent, ready to act.',
      lead:
        'Ask from a surface you already use. Lobster0 understands intent, checks boundaries, requests approval, and finishes the work on your machine.',
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
        permissionModes: 'permission modes',
        dataLocation: 'data stays',
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
      eyebrow: 'HOW IT RUNS / 03',
      title: 'What it does when things go wrong.',
      lead: 'Denied, failed, disconnected — an agent earns trust in these three moments, not in the happy path.',
    },
    workflows: [
      {
        id: 'denied',
        label: 'Approval denied',
        title: 'You say no, and it actually stops.',
        summary: 'A denial is not "rephrase and retry". The rejected arguments are void on the spot — no detour, no retry, no quietly splitting the task into smaller steps.',
        flow: [
          { icon: 'intent', label: 'Request', detail: '"clean out /tmp"', state: 'default' },
          { icon: 'argv', label: 'Arguments locked', detail: 'rm -rf /tmp/*', state: 'default' },
          { icon: 'gate', label: 'Owner denies', detail: 'tapped "Deny"', state: 'blocked' },
          { icon: 'stop', label: 'Stopped', detail: 'these argv are void', state: 'blocked' },
        ],
      },
      {
        id: 'crash',
        label: 'Execution fails',
        title: 'Failure stops where it happened.',
        summary: 'A failed tool stays failed. Exit code, stderr, and which step broke all come back verbatim — not smoothed over into "done".',
        flow: [
          { icon: 'program', label: 'Program', detail: 'git push origin main', state: 'default' },
          { icon: 'run', label: 'Subprocess exits', detail: 'exit code 128', state: 'blocked' },
          { icon: 'argv', label: 'Raw stderr', detail: 'rejected: non-fast-forward', state: 'default' },
          { icon: 'result', label: 'Reported as-is', detail: 'where and why it failed', state: 'done' },
        ],
      },
      {
        id: 'isolation',
        label: 'Fault isolation',
        title: 'One surface goes down, the rest keep running.',
        summary: 'When the Feishu gateway drops, the TUI, Telegram, and Discord queues and delivery state are untouched — capability is shared, the failure domain is not.',
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
