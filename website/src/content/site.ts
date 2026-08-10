export type Locale = 'zh-CN' | 'en' | 'ja' | 'ko' | 'fr';
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
      dataLocationValue: string;
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
  ui: {
    boundaryMoment: string;
    primaryNav: string;
    footerNav: string;
    requirements: string;
    runtimePath: string;
    healthy: string;
    sharedCore: string;
    sharedCoreShort: string;
    coreStack: string;
    coreStackShort: string;
    edgeSummary: string;
    channelsAria: string;
    isolationAria: string;
    transport: string;
    delivery: string;
    queue: string;
    failure: string;
    isolated: string;
    contained: string;
    deniedAria: string;
    crashAria: string;
    safetyAria: string;
    workSurface: string;
    safetyAsk: string;
    safetyReply: string;
    awaitingConfirm: string;
    checkWorkspace: string;
    checkCommand: string;
    checkNetwork: string;
    checkSecret: string;
    automationAria: string;
    communitySurface: string;
    automationFired: string;
    automationBlocked: string;
    automationOffCopy: string;
    automationEnabled: string;
    automationDisabled: string;
    onceEnabled: string;
    ownerEnables: string;
    policyGate: string;
    versionedCases: string;
    memoryAria: string;
    ownerBoundary: string;
    longTerm: string;
    shortTerm: string;
    promote: string;
    memoryName: string;
    memoryNameValue: string;
    memoryLang: string;
    memoryLangValue: string;
    memoryHabit: string;
    memoryHabitValue: string;
    memoryProject: string;
    memoryProjectValue: string;
    memoryDoing: string;
    memoryDoingValue: string;
    memoryJustSaid: string;
    memoryJustSaidValue: string;
    memoryOnEnd: string;
    memoryOnEndValue: string;
    steps: string;
    source: string;
    sourceValue: string;
    state: string;
    stateValue: string;
    runtime: string;
    surfaces: string;
    cases: string;
    shared: string;
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
        dataLocationValue: '本机',
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
    ui: {
      boundaryMoment: '边界时刻',
      primaryNav: '主导航',
      footerNav: '页脚导航',
      requirements: '运行要求',
      runtimePath: 'Lobster0 运行路径',
      healthy: '健康',
      sharedCore: '共享核心',
      sharedCoreShort: '共享核心',
      coreStack: 'Agent · 策略 · 工具 · 记忆',
      coreStackShort: 'Python Core / 策略 / 记忆',
      edgeSummary: '传输 · 交付 · 队列',
      channelsAria: '一个 AgentRuntime 与四个隔离入口',
      isolationAria: '共享 AgentRuntime 与隔离入口',
      transport: '传输',
      delivery: '交付',
      queue: '队列',
      failure: '故障',
      isolated: '隔离',
      contained: '受控',
      deniedAria: '请求 → 参数锁定 → 拒绝 → 终止',
      crashAria: '程序 → 退出码 → stderr → 如实回报',
      safetyAria: '飞书里的一次 SAFE 审批',
      workSurface: '工作入口',
      safetyAsk: '帮我清一下 /tmp 下的临时文件',
      safetyReply: '检测到高风险操作，需要你确认 exact argv 后再执行',
      awaitingConfirm: '等待确认',
      checkWorkspace: '工作区',
      checkCommand: '命令',
      checkNetwork: '网络',
      checkSecret: '密钥',
      automationAria: 'Discord 里一次被拦截的定时任务',
      communitySurface: '社区入口',
      automationFired: '定时任务 daily-report 已触发',
      automationBlocked: '已拦截',
      automationOffCopy: '自动化默认关闭，需要 Owner 显式启用',
      automationEnabled: '已开启',
      automationDisabled: '默认关闭',
      onceEnabled: '一旦开启',
      ownerEnables: '所有者启用',
      policyGate: '策略门禁',
      versionedCases: '条版本化场景',
      memoryAria: 'Markdown 事实源与 SQLite 投影',
      ownerBoundary: '所有者边界 · 本地工作区',
      longTerm: '长期记忆 · 一直记得',
      shortTerm: '短期记忆 · 本次对话',
      promote: '沉淀',
      memoryName: '称呼',
      memoryNameValue: '叫我 Ned',
      memoryLang: '语言',
      memoryLangValue: '默认用中文回复',
      memoryHabit: '习惯',
      memoryHabitValue: '删文件前先问我',
      memoryProject: '项目',
      memoryProjectValue: '主力仓库是 lobster0',
      memoryDoing: '正在做',
      memoryDoingValue: '改官网 Logo',
      memoryJustSaid: '刚提到',
      memoryJustSaidValue: '钳子要从胸口伸出',
      memoryOnEnd: '会话结束',
      memoryOnEndValue: '重要的沉淀为长期',
      steps: '步骤',
      source: '来源',
      sourceValue: '真实执行链路',
      state: '状态',
      stateValue: '实现证据',
      runtime: '运行时',
      surfaces: '入口',
      cases: '场景',
      shared: '共享',
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
        dataLocationValue: 'local',
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
    ui: {
      boundaryMoment: 'BOUNDARY MOMENT',
      primaryNav: 'Primary',
      footerNav: 'Footer',
      requirements: 'Requirements',
      runtimePath: 'Lobster0 runtime path',
      healthy: 'healthy',
      sharedCore: 'SHARED CORE',
      sharedCoreShort: 'SHARED',
      coreStack: 'Agent · Policy · Tools · Memory',
      coreStackShort: 'Python Core / Policy / Memory',
      edgeSummary: 'transport · delivery · queue',
      channelsAria: 'One AgentRuntime and four isolated surfaces',
      isolationAria: 'Shared AgentRuntime with isolated channel edges',
      transport: 'Transport',
      delivery: 'Delivery',
      queue: 'queue',
      failure: 'failure',
      isolated: 'isolated',
      contained: 'contained',
      deniedAria: 'request → arguments locked → denied → stopped',
      crashAria: 'program → exit code → stderr → reported as-is',
      safetyAria: 'A SAFE approval inside Feishu',
      workSurface: 'work surface',
      safetyAsk: 'Clean up the temp files under /tmp',
      safetyReply: 'This is a high-risk action — confirm the exact argv before I run it',
      awaitingConfirm: 'awaiting confirm',
      checkWorkspace: 'WORKSPACE',
      checkCommand: 'COMMAND',
      checkNetwork: 'NETWORK',
      checkSecret: 'SECRET',
      automationAria: 'A blocked scheduled task inside Discord',
      communitySurface: 'community surface',
      automationFired: 'Scheduled task daily-report just fired',
      automationBlocked: 'blocked',
      automationOffCopy: 'automation is off by default — needs explicit owner opt-in',
      automationEnabled: 'ENABLED',
      automationDisabled: 'DISABLED BY DEFAULT',
      onceEnabled: 'ONCE ENABLED',
      ownerEnables: 'owner enables',
      policyGate: 'policy gate',
      versionedCases: 'versioned cases',
      memoryAria: 'Markdown truth and SQLite projection',
      ownerBoundary: 'owner boundary · local workspace',
      longTerm: 'LONG-TERM · always remembered',
      shortTerm: 'SHORT-TERM · this session',
      promote: 'promote',
      memoryName: 'Name',
      memoryNameValue: 'Call me Ned',
      memoryLang: 'Language',
      memoryLangValue: 'Reply in Chinese',
      memoryHabit: 'Habit',
      memoryHabitValue: 'Ask before deleting files',
      memoryProject: 'Project',
      memoryProjectValue: 'Main repo is lobster0',
      memoryDoing: 'Doing',
      memoryDoingValue: 'Reworking the site logo',
      memoryJustSaid: 'Just said',
      memoryJustSaidValue: 'Claws should come from the chest',
      memoryOnEnd: 'On session end',
      memoryOnEndValue: 'Important bits promoted',
      steps: 'STEPS',
      source: 'SOURCE',
      sourceValue: 'real execution path',
      state: 'STATE',
      stateValue: 'implementation evidence',
      runtime: 'RUNTIME',
      surfaces: 'SURFACES',
      cases: 'CASES',
      shared: 'shared',
    },
  },
  ja: {
    meta: {
      title: 'Lobster0 — 手元で動く、あなたのエージェント。',
      description:
        'ローカル優先で境界を検査できるオープンソースの個人エージェント。TUI、Feishu、Telegram、Discord を単一の Python コアで安全に動かします。',
    },
    nav: {
      product: '機能',
      workbench: '動作の仕組み',
      docs: 'ドキュメント',
      github: 'GitHub',
      language: '言語を切り替える',
    },
    hero: {
      eyebrow: 'ローカル優先 / オープンソース / 境界を検査できる',
      title: '手元で動く、あなたのエージェント。',
      lead:
        '使い慣れた入口からリクエストを送るだけ。Lobster0 はあなたのマシン上で意図を読み取り、境界を確認し、承認を求めたうえで、仕事を安全に終わらせます。',
      primaryCta: '5 分で始める',
      secondaryCta: 'ソースを見る',
      installLabel: 'ソースから起動',
      copyLabel: 'コマンドをコピー',
      copiedLabel: 'コピーしました',
      surfaces: [
        { name: 'TUI', role: 'ローカル', note: '完全な制御' },
        { name: 'Feishu', role: '仕事の入口', note: 'チーム連携' },
        { name: 'Telegram', role: 'モバイル', note: 'いつでも届く' },
        { name: 'Discord', role: 'コミュニティ', note: '独立した配信' },
      ],
    },
    trace: {
      ariaLabel: 'Lobster0 実行トレース',
      eyebrow: '実行トレース / 01',
      title: '実行はブラックボックスではない。',
      description: '1 つのリクエストが実行時の 6 つの状態を通過し、各段階に境界・結果・責任者があります。',
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
        'Channel ケース 33 件、Automation ケース 15 件（いずれもバージョン管理下）。Implementation PASS は Live PASS ではありません。',
      labels: {
        core: 'Python コア',
        surfaces: '入口',
        tools: '内蔵ツール',
        permissionModes: '権限モード',
        dataLocation: 'データの保管先',
        dataLocationValue: 'ローカル',
      },
    },
    product: {
      eyebrow: '機能 / 02',
      title: '1 つのコア、検査できる 5 つの側面。',
      lead: 'プロダクトのように読みやすく、OSS のように具体的に。機能を切り替えて構造・境界・検証の根拠をそのまま確認できます。',
    },
    capabilities: [
      {
        id: 'runtime',
        eyebrow: '01 / コアループ',
        label: 'ランタイム',
        title: 'メッセージから結果まで、制御された 1 本の連鎖。',
        summary: 'モデルが意図を読み、Policy が境界を決め、Approval が引数を固定し、そのうえで初めて Tool が動きます。',
        facts: ['6 段階の Trace 順序', 'OpenAI 互換プロバイダ', '結果は元の会話へ', '構造化された実行記録'],
      },
      {
        id: 'channels',
        eyebrow: '02 / 隔離された入口',
        label: 'マルチ入口',
        title: '4 つの入口が 1 つの AgentRuntime を共有。',
        summary: 'TUI・Feishu・Telegram・Discord は能力を共有しますが、障害範囲と配信状態は互いに隔離されています。',
        facts: ['4 つの入口', 'バージョン管理された 33 ケース', 'Transport の隔離', 'Delivery とキューの隔離'],
      },
      {
        id: 'safety',
        eyebrow: '03 / 実行前の検査',
        label: '安全境界',
        title: '制限は実行の前に効く。',
        summary: 'Workspace・exact argv・ネットワーク・シークレットはすべて単一の Policy を通ります。プロンプト頼みではありません。',
        facts: ['4 段階の権限モード', '引数に紐づく承認', 'Workspace の境界', 'SSRF とシークレットの防御'],
      },
      {
        id: 'memory',
        eyebrow: '04 / 所有者の境界',
        label: '記憶',
        title: 'Markdown が真実、SQLite は投影。',
        summary: '長期の事実は読めて、確認できて、あなたの管理下にあります。構造化インデックスはいつでも再構築できます。',
        facts: ['所有者の境界', 'Markdown が真実', 'SQLite コントロールプレーン', '同じ所有者の間でのみ共有'],
      },
      {
        id: 'automation',
        eyebrow: '05 / 明示的なゲート',
        label: '自動化',
        title: '既定はオフ。許可して初めて自動化。',
        summary: '自動実行は明示的な有効化と安全ゲートを通過し、その後バージョン管理されたケースで実装の意味を検証します。',
        facts: ['既定で無効', 'バージョン管理された 15 ケース', '明示的な認可ゲート', 'Implementation ≠ Live'],
      },
    ],
    workbench: {
      eyebrow: '動作の仕組み / 03',
      title: 'うまくいかないとき、どう振る舞うか。',
      lead: '拒否されたとき、失敗したとき、切断されたとき。エージェントの信頼はこの 3 つの瞬間で決まります。',
    },
    workflows: [
      {
        id: 'denied',
        label: '承認を拒否',
        title: '「いいえ」と言えば、本当に止まる。',
        summary: '拒否は「言い方を変えて再試行」ではありません。却下された引数はその場で無効になり、迂回も再試行も、こっそり小さな手順に分割することもしません。',
        flow: [
          { icon: 'intent', label: 'リクエスト', detail: '「/tmp を掃除して」', state: 'default' },
          { icon: 'argv', label: '引数を固定', detail: 'rm -rf /tmp/*', state: 'default' },
          { icon: 'gate', label: '所有者が拒否', detail: '「許可しない」を選択', state: 'blocked' },
          { icon: 'stop', label: 'その場で停止', detail: 'この argv は無効', state: 'blocked' },
        ],
      },
      {
        id: 'crash',
        label: '実行が失敗',
        title: '失敗はその場で止まる。',
        summary: '失敗したツールは失敗のままです。終了コード・stderr・どの段階で壊れたかがそのまま返り、「完了しました」に丸められることはありません。',
        flow: [
          { icon: 'program', label: 'プログラム', detail: 'git push origin main', state: 'default' },
          { icon: 'run', label: 'サブプロセス終了', detail: 'exit code 128', state: 'blocked' },
          { icon: 'argv', label: '生の stderr', detail: 'rejected: non-fast-forward', state: 'default' },
          { icon: 'result', label: 'そのまま報告', detail: '失敗の箇所と理由', state: 'done' },
        ],
      },
      {
        id: 'isolation',
        label: '障害の隔離',
        title: '1 つの入口が落ちても、他は動き続ける。',
        summary: 'Feishu ゲートウェイが切断されても、TUI・Telegram・Discord のキューと配信状態は影響を受けません。共有するのは能力であり、障害範囲ではありません。',
      },
    ],
    quickStart: {
      eyebrow: 'ローカルで動かす',
      title: 'ローカルで始めて、最初の Trace を見る。',
      lead: 'Python 3.12+、既定の TUI は Node.js 22.19+ が必要です。サービスの認証情報は常にあなたが保持します。',
      docsCta: 'インストール手順を読む',
      githubCta: 'コントリビュート',
    },
    footer: {
      statement: '小さなコア、明確な境界、あなたのマシン。',
      docs: 'ドキュメント',
      issues: 'Issues',
      source: 'ソース',
    },
    ui: {
      boundaryMoment: '境界の瞬間',
      primaryNav: 'メインナビ',
      footerNav: 'フッター',
      requirements: '動作要件',
      runtimePath: 'Lobster0 の実行経路',
      healthy: '正常',
      sharedCore: '共有コア',
      sharedCoreShort: '共有',
      coreStack: 'Agent · Policy · Tools · Memory',
      coreStackShort: 'Python Core / Policy / Memory',
      edgeSummary: 'transport · delivery · queue',
      channelsAria: '1 つの AgentRuntime と 4 つの隔離された入口',
      isolationAria: '共有 AgentRuntime と隔離されたチャネル',
      transport: 'Transport',
      delivery: 'Delivery',
      queue: 'キュー',
      failure: '障害',
      isolated: '隔離',
      contained: '封じ込め',
      deniedAria: 'リクエスト → 引数固定 → 拒否 → 停止',
      crashAria: 'プログラム → 終了コード → stderr → そのまま報告',
      safetyAria: 'Feishu 上での SAFE 承認',
      workSurface: '仕事の入口',
      safetyAsk: '/tmp の一時ファイルを片付けて',
      safetyReply: '高リスクな操作を検出しました。exact argv を確認してから実行します',
      awaitingConfirm: '確認待ち',
      checkWorkspace: 'ワークスペース',
      checkCommand: 'コマンド',
      checkNetwork: 'ネットワーク',
      checkSecret: 'シークレット',
      automationAria: 'Discord 上でブロックされた定期実行',
      communitySurface: 'コミュニティの入口',
      automationFired: '定期実行 daily-report が発火しました',
      automationBlocked: 'ブロック済み',
      automationOffCopy: '自動化は既定でオフ。所有者の明示的な許可が必要です',
      automationEnabled: '有効',
      automationDisabled: '既定で無効',
      onceEnabled: '有効にすると',
      ownerEnables: '所有者が有効化',
      policyGate: 'ポリシーゲート',
      versionedCases: '件のバージョン管理ケース',
      memoryAria: 'Markdown の真実と SQLite の投影',
      ownerBoundary: '所有者の境界 · ローカル作業領域',
      longTerm: '長期記憶 · ずっと覚えている',
      shortTerm: '短期記憶 · 今回の会話',
      promote: '定着',
      memoryName: '呼び方',
      memoryNameValue: 'Ned と呼んで',
      memoryLang: '言語',
      memoryLangValue: '既定は中国語で返信',
      memoryHabit: '習慣',
      memoryHabitValue: 'ファイル削除の前に確認',
      memoryProject: 'プロジェクト',
      memoryProjectValue: '主なリポジトリは lobster0',
      memoryDoing: '作業中',
      memoryDoingValue: 'サイトのロゴを修正中',
      memoryJustSaid: '直前の発言',
      memoryJustSaidValue: 'ハサミは胸から出す',
      memoryOnEnd: '会話終了時',
      memoryOnEndValue: '重要な内容を長期へ',
      steps: 'ステップ',
      source: '出典',
      sourceValue: '実際の実行経路',
      state: '状態',
      stateValue: '実装の根拠',
      runtime: 'ランタイム',
      surfaces: '入口',
      cases: 'ケース',
      shared: '共有',
    },
  },
  ko: {
    meta: {
      title: 'Lobster0 — 내 컴퓨터에서 움직이는 에이전트.',
      description:
        '로컬 우선, 경계를 검사할 수 있는 오픈소스 개인 에이전트. 하나의 Python 코어로 TUI, Feishu, Telegram, Discord에서 안전하게 실행됩니다.',
    },
    nav: {
      product: '기능',
      workbench: '동작 방식',
      docs: '문서',
      github: 'GitHub',
      language: '언어 변경',
    },
    hero: {
      eyebrow: '로컬 우선 / 오픈소스 / 검사 가능한 경계',
      title: '내 컴퓨터에서 움직이는 에이전트.',
      lead:
        '늘 쓰던 입구에서 요청만 보내세요. Lobster0는 여러분의 컴퓨터에서 의도를 읽고, 경계를 확인하고, 승인을 요청한 뒤 작업을 안전하게 끝냅니다.',
      primaryCta: '5분 만에 시작',
      secondaryCta: '소스 보기',
      installLabel: '소스에서 실행',
      copyLabel: '명령 복사',
      copiedLabel: '복사됨',
      surfaces: [
        { name: 'TUI', role: '로컬', note: '완전한 제어' },
        { name: 'Feishu', role: '업무 입구', note: '팀 협업' },
        { name: 'Telegram', role: '모바일', note: '언제나 연결' },
        { name: 'Discord', role: '커뮤니티', note: '독립 전달' },
      ],
    },
    trace: {
      ariaLabel: 'Lobster0 실행 추적',
      eyebrow: '실행 추적 / 01',
      title: '실행은 블랙박스가 아니다.',
      description: '하나의 요청이 런타임의 여섯 가지 상태를 통과하며, 각 단계마다 경계와 결과와 책임자가 있습니다.',
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
        '버전 관리된 Channel 케이스 33건, Automation 케이스 15건. Implementation PASS는 Live PASS가 아닙니다.',
      labels: {
        core: 'Python 코어',
        surfaces: '입구',
        tools: '내장 도구',
        permissionModes: '권한 등급',
        dataLocation: '데이터 위치',
        dataLocationValue: '로컬',
      },
    },
    product: {
      eyebrow: '기능 / 02',
      title: '하나의 코어, 검사 가능한 다섯 개의 면.',
      lead: '제품처럼 읽기 쉽고, 오픈소스처럼 구체적으로. 기능을 전환해 구조와 경계와 검증 근거를 바로 확인하세요.',
    },
    capabilities: [
      {
        id: 'runtime',
        eyebrow: '01 / 코어 루프',
        label: '런타임',
        title: '메시지에서 결과까지, 통제된 하나의 실행 사슬.',
        summary: '모델이 의도를 읽고, Policy가 경계를 정하고, Approval이 인자를 고정한 뒤에야 Tool이 실행됩니다.',
        facts: ['6단계 Trace 순서', 'OpenAI 호환 프로바이더', '결과는 원래 대화로', '구조화된 실행 기록'],
      },
      {
        id: 'channels',
        eyebrow: '02 / 격리된 입구',
        label: '다중 입구',
        title: '네 개의 입구가 하나의 AgentRuntime을 공유.',
        summary: 'TUI, Feishu, Telegram, Discord는 능력을 공유하지만 장애 범위와 전달 상태는 서로 격리됩니다.',
        facts: ['4개 입구', '버전 관리된 33개 케이스', 'Transport 격리', 'Delivery와 큐 격리'],
      },
      {
        id: 'safety',
        eyebrow: '03 / 실행 전 검사',
        label: '안전 경계',
        title: '제한은 실행보다 먼저 걸린다.',
        summary: 'Workspace, exact argv, 네트워크, 시크릿이 모두 하나의 Policy를 거칩니다. 프롬프트에 기대지 않습니다.',
        facts: ['4단계 권한 모드', '인자에 묶인 승인', 'Workspace 경계', 'SSRF와 시크릿 방어'],
      },
      {
        id: 'memory',
        eyebrow: '04 / 소유자 경계',
        label: '기억',
        title: 'Markdown이 진실, SQLite는 투영.',
        summary: '장기 사실은 읽을 수 있고 검토할 수 있으며 소유자가 통제합니다. 구조화 인덱스는 언제든 다시 만들 수 있습니다.',
        facts: ['소유자 경계', 'Markdown 진실', 'SQLite 컨트롤 플레인', '같은 소유자끼리만 공유'],
      },
      {
        id: 'automation',
        eyebrow: '05 / 명시적 게이트',
        label: '자동화',
        title: '기본은 꺼짐. 허용해야 자동화된다.',
        summary: '자동 실행은 명시적 활성화와 안전 게이트를 거친 뒤, 버전 관리된 케이스로 구현 의미를 검증합니다.',
        facts: ['기본 비활성화', '버전 관리된 15개 케이스', '명시적 인가 게이트', 'Implementation ≠ Live'],
      },
    ],
    workbench: {
      eyebrow: '동작 방식 / 03',
      title: '문제가 생겼을 때 어떻게 하는가.',
      lead: '거절당했을 때, 실패했을 때, 끊겼을 때 — 에이전트의 신뢰는 이 세 순간에 결정됩니다.',
    },
    workflows: [
      {
        id: 'denied',
        label: '승인 거절',
        title: '아니라고 하면, 정말 멈춘다.',
        summary: '거절은 "다르게 말해서 다시 시도"가 아닙니다. 거절된 인자는 그 자리에서 무효가 되고, 우회도 재시도도, 작은 단계로 몰래 쪼개는 일도 없습니다.',
        flow: [
          { icon: 'intent', label: '요청', detail: '"/tmp 정리해줘"', state: 'default' },
          { icon: 'argv', label: '인자 고정', detail: 'rm -rf /tmp/*', state: 'default' },
          { icon: 'gate', label: '소유자 거절', detail: '"허용 안 함" 선택', state: 'blocked' },
          { icon: 'stop', label: '즉시 중단', detail: '이 argv는 무효', state: 'blocked' },
        ],
      },
      {
        id: 'crash',
        label: '실행 실패',
        title: '실패는 그 자리에서 멈춘다.',
        summary: '실패한 도구는 실패한 채로 남습니다. 종료 코드와 stderr, 어느 단계에서 깨졌는지가 그대로 돌아오며 "완료"로 포장되지 않습니다.',
        flow: [
          { icon: 'program', label: '프로그램', detail: 'git push origin main', state: 'default' },
          { icon: 'run', label: '서브프로세스 종료', detail: 'exit code 128', state: 'blocked' },
          { icon: 'argv', label: '원본 stderr', detail: 'rejected: non-fast-forward', state: 'default' },
          { icon: 'result', label: '사실대로 보고', detail: '실패 지점과 이유', state: 'done' },
        ],
      },
      {
        id: 'isolation',
        label: '장애 격리',
        title: '한 입구가 죽어도, 나머지는 돈다.',
        summary: 'Feishu 게이트웨이가 끊겨도 TUI, Telegram, Discord의 큐와 전달 상태는 영향을 받지 않습니다. 공유하는 것은 능력이지 장애 범위가 아닙니다.',
      },
    ],
    quickStart: {
      eyebrow: '로컬에서 실행',
      title: '로컬에서 시작해 첫 Trace를 확인하세요.',
      lead: 'Python 3.12+, 기본 TUI는 Node.js 22.19+가 필요합니다. 서비스 자격 증명은 항상 여러분이 보관합니다.',
      docsCta: '설치 가이드 읽기',
      githubCta: '기여하기',
    },
    footer: {
      statement: '작은 코어, 명확한 경계, 당신의 컴퓨터.',
      docs: '문서',
      issues: 'Issues',
      source: '소스',
    },
    ui: {
      boundaryMoment: '경계의 순간',
      primaryNav: '메인 내비게이션',
      footerNav: '푸터',
      requirements: '실행 요구사항',
      runtimePath: 'Lobster0 실행 경로',
      healthy: '정상',
      sharedCore: '공유 코어',
      sharedCoreShort: '공유',
      coreStack: 'Agent · Policy · Tools · Memory',
      coreStackShort: 'Python Core / Policy / Memory',
      edgeSummary: 'transport · delivery · queue',
      channelsAria: '하나의 AgentRuntime과 네 개의 격리된 입구',
      isolationAria: '공유 AgentRuntime과 격리된 채널',
      transport: 'Transport',
      delivery: 'Delivery',
      queue: '큐',
      failure: '장애',
      isolated: '격리',
      contained: '통제됨',
      deniedAria: '요청 → 인자 고정 → 거절 → 중단',
      crashAria: '프로그램 → 종료 코드 → stderr → 사실대로 보고',
      safetyAria: 'Feishu 안에서의 SAFE 승인',
      workSurface: '업무 입구',
      safetyAsk: '/tmp의 임시 파일 좀 정리해줘',
      safetyReply: '고위험 작업을 감지했습니다. exact argv를 확인한 뒤 실행합니다',
      awaitingConfirm: '확인 대기 중',
      checkWorkspace: '작업 공간',
      checkCommand: '명령',
      checkNetwork: '네트워크',
      checkSecret: '시크릿',
      automationAria: 'Discord에서 차단된 예약 작업',
      communitySurface: '커뮤니티 입구',
      automationFired: '예약 작업 daily-report가 실행되었습니다',
      automationBlocked: '차단됨',
      automationOffCopy: '자동화는 기본으로 꺼져 있으며 소유자의 명시적 허용이 필요합니다',
      automationEnabled: '활성화됨',
      automationDisabled: '기본 비활성화',
      onceEnabled: '활성화하면',
      ownerEnables: '소유자 활성화',
      policyGate: '정책 게이트',
      versionedCases: '개 버전 관리 케이스',
      memoryAria: 'Markdown 진실과 SQLite 투영',
      ownerBoundary: '소유자 경계 · 로컬 작업 공간',
      longTerm: '장기 기억 · 계속 기억함',
      shortTerm: '단기 기억 · 이번 대화',
      promote: '승격',
      memoryName: '호칭',
      memoryNameValue: 'Ned라고 불러줘',
      memoryLang: '언어',
      memoryLangValue: '기본은 중국어로 답변',
      memoryHabit: '습관',
      memoryHabitValue: '파일 삭제 전에 먼저 확인',
      memoryProject: '프로젝트',
      memoryProjectValue: '주력 저장소는 lobster0',
      memoryDoing: '작업 중',
      memoryDoingValue: '사이트 로고 수정',
      memoryJustSaid: '방금 언급',
      memoryJustSaidValue: '집게는 가슴에서 나와야 함',
      memoryOnEnd: '대화 종료 시',
      memoryOnEndValue: '중요한 내용은 장기로',
      steps: '단계',
      source: '출처',
      sourceValue: '실제 실행 경로',
      state: '상태',
      stateValue: '구현 근거',
      runtime: '런타임',
      surfaces: '입구',
      cases: '케이스',
      shared: '공유',
    },
  },
  fr: {
    meta: {
      title: 'Lobster0 — votre agent local, prêt à agir.',
      description:
        'Un agent personnel open source, local d’abord, aux frontières inspectables : un seul cœur Python pour TUI, Feishu, Telegram et Discord.',
    },
    nav: {
      product: 'Fonctions',
      workbench: 'Fonctionnement',
      docs: 'Docs',
      github: 'GitHub',
      language: 'Changer de langue',
    },
    hero: {
      eyebrow: 'LOCAL D’ABORD / OPEN SOURCE / FRONTIÈRES INSPECTABLES',
      title: 'Votre agent local, prêt à agir.',
      lead:
        'Envoyez une demande depuis une interface que vous utilisez déjà. Lobster0 lit l’intention sur votre machine, vérifie la frontière, demande votre accord, puis termine la tâche en toute sécurité.',
      primaryCta: 'Démarrer en 5 minutes',
      secondaryCta: 'Voir le code',
      installLabel: 'Lancer depuis les sources',
      copyLabel: 'Copier les commandes',
      copiedLabel: 'Copié',
      surfaces: [
        { name: 'TUI', role: 'Local', note: 'Contrôle total' },
        { name: 'Feishu', role: 'Travail', note: 'Contexte d’équipe' },
        { name: 'Telegram', role: 'Mobile', note: 'Toujours joignable' },
        { name: 'Discord', role: 'Communauté', note: 'Livraison isolée' },
      ],
    },
    trace: {
      ariaLabel: 'Trace d’exécution Lobster0',
      eyebrow: 'TRACE D’EXÉCUTION / 01',
      title: 'L’exécution n’est pas une boîte noire.',
      description: 'Une requête traverse six états réels du runtime, chacun avec sa frontière, son résultat et son responsable.',
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
        '33 cas Channel et 15 cas Automation versionnés. Implementation PASS n’est pas Live PASS.',
      labels: {
        core: 'Cœur Python',
        surfaces: 'interfaces',
        tools: 'outils intégrés',
        permissionModes: 'modes de permission',
        dataLocation: 'données stockées',
        dataLocationValue: 'local',
      },
    },
    product: {
      eyebrow: 'FONCTIONS / 02',
      title: 'Un cœur, cinq facettes inspectables.',
      lead: 'Lisible comme un produit, concret comme un projet open source. Changez de facette pour inspecter structure, frontières et preuves de vérification.',
    },
    capabilities: [
      {
        id: 'runtime',
        eyebrow: '01 / BOUCLE CENTRALE',
        label: 'Runtime',
        title: 'Du message au résultat, une chaîne contrôlée.',
        summary: 'Le modèle lit l’intention ; Policy fixe la frontière ; Approval verrouille les arguments ; alors seulement un Tool s’exécute.',
        facts: ['6 étapes de Trace ordonnées', 'Fournisseur compatible OpenAI', 'Le résultat revient à la conversation', 'Journal d’exécution structuré'],
      },
      {
        id: 'channels',
        eyebrow: '02 / INTERFACES ISOLÉES',
        label: 'Multi-interface',
        title: 'Quatre interfaces, un seul AgentRuntime.',
        summary: 'TUI, Feishu, Telegram et Discord partagent les capacités, mais les domaines de panne et les états de livraison restent isolés.',
        facts: ['4 interfaces', '33 cas versionnés', 'Isolation du transport', 'Isolation livraison et file'],
      },
      {
        id: 'safety',
        eyebrow: '03 / POLICY AVANT ACTION',
        label: 'Frontière de sécurité',
        title: 'La limite s’applique avant l’action.',
        summary: 'Workspace, exact argv, réseau et secrets passent par une seule Policy, au lieu de dépendre de la discipline du prompt.',
        facts: ['4 modes de permission', 'Approbation liée aux arguments', 'Frontière du workspace', 'Protections SSRF et secrets'],
      },
      {
        id: 'memory',
        eyebrow: '04 / FRONTIÈRE DU PROPRIÉTAIRE',
        label: 'Mémoire',
        title: 'Markdown fait foi, SQLite est la projection.',
        summary: 'Les faits durables restent lisibles, vérifiables et sous votre contrôle ; l’index structuré peut être reconstruit à tout moment.',
        facts: ['Frontière du propriétaire', 'Markdown fait foi', 'Plan de contrôle SQLite', 'Partagé au sein d’un seul propriétaire'],
      },
      {
        id: 'automation',
        eyebrow: '05 / PORTE EXPLICITE',
        label: 'Automatisation',
        title: 'Désactivée par défaut. Automatisée seulement après votre accord.',
        summary: 'Les exécutions automatiques passent une activation explicite et une porte de sécurité, puis des cas versionnés vérifient la sémantique implémentée.',
        facts: ['Désactivée par défaut', '15 cas versionnés', 'Porte d’autorisation explicite', 'Implementation ≠ Live'],
      },
    ],
    workbench: {
      eyebrow: 'FONCTIONNEMENT / 03',
      title: 'Ce qu’il fait quand ça tourne mal.',
      lead: 'Refusé, échoué, déconnecté — un agent gagne la confiance dans ces trois moments, pas dans le cas idéal.',
    },
    workflows: [
      {
        id: 'denied',
        label: 'Refus d’approbation',
        title: 'Vous dites non, et il s’arrête vraiment.',
        summary: 'Un refus n’est pas « reformule et réessaie ». Les arguments rejetés sont annulés sur-le-champ — aucun détour, aucune relance, aucun découpage discret en petites étapes.',
        flow: [
          { icon: 'intent', label: 'Requête', detail: '« nettoie /tmp »', state: 'default' },
          { icon: 'argv', label: 'Arguments verrouillés', detail: 'rm -rf /tmp/*', state: 'default' },
          { icon: 'gate', label: 'Le propriétaire refuse', detail: '« Refuser » sélectionné', state: 'blocked' },
          { icon: 'stop', label: 'Arrêt immédiat', detail: 'ces argv sont annulés', state: 'blocked' },
        ],
      },
      {
        id: 'crash',
        label: 'Échec d’exécution',
        title: 'L’échec s’arrête là où il se produit.',
        summary: 'Un outil qui échoue reste en échec. Code de sortie, stderr et étape fautive reviennent tels quels — jamais lissés en « terminé ».',
        flow: [
          { icon: 'program', label: 'Programme', detail: 'git push origin main', state: 'default' },
          { icon: 'run', label: 'Sortie du sous-processus', detail: 'exit code 128', state: 'blocked' },
          { icon: 'argv', label: 'stderr brut', detail: 'rejected: non-fast-forward', state: 'default' },
          { icon: 'result', label: 'Rapporté tel quel', detail: 'où et pourquoi ça a échoué', state: 'done' },
        ],
      },
      {
        id: 'isolation',
        label: 'Isolation des pannes',
        title: 'Une interface tombe, les autres continuent.',
        summary: 'Quand la passerelle Feishu se coupe, les files et états de livraison de TUI, Telegram et Discord restent intacts — la capacité est partagée, pas le domaine de panne.',
      },
    ],
    quickStart: {
      eyebrow: 'LANCEZ-LE VOUS-MÊME',
      title: 'Démarrez en local. Voyez votre première Trace.',
      lead: 'Python 3.12+ ; le TUI par défaut nécessite Node.js 22.19+. Vous gardez tous vos identifiants de service.',
      docsCta: 'Lire le guide d’installation',
      githubCta: 'Contribuer',
    },
    footer: {
      statement: 'Petit cœur. Frontières explicites. Votre machine.',
      docs: 'Docs',
      issues: 'Issues',
      source: 'Code source',
    },
    ui: {
      boundaryMoment: 'MOMENT DE FRONTIÈRE',
      primaryNav: 'Navigation principale',
      footerNav: 'Pied de page',
      requirements: 'Prérequis',
      runtimePath: 'Chemin d’exécution Lobster0',
      healthy: 'sain',
      sharedCore: 'CŒUR PARTAGÉ',
      sharedCoreShort: 'PARTAGÉ',
      coreStack: 'Agent · Policy · Tools · Memory',
      coreStackShort: 'Python Core / Policy / Memory',
      edgeSummary: 'transport · livraison · file',
      channelsAria: 'Un AgentRuntime et quatre interfaces isolées',
      isolationAria: 'AgentRuntime partagé avec des canaux isolés',
      transport: 'Transport',
      delivery: 'Livraison',
      queue: 'file',
      failure: 'panne',
      isolated: 'isolé',
      contained: 'contenu',
      deniedAria: 'requête → arguments verrouillés → refus → arrêt',
      crashAria: 'programme → code de sortie → stderr → rapporté tel quel',
      safetyAria: 'Une approbation SAFE dans Feishu',
      workSurface: 'interface de travail',
      safetyAsk: 'Nettoie les fichiers temporaires dans /tmp',
      safetyReply: 'Action à haut risque détectée — confirmez l’exact argv avant que je l’exécute',
      awaitingConfirm: 'en attente de confirmation',
      checkWorkspace: 'WORKSPACE',
      checkCommand: 'COMMANDE',
      checkNetwork: 'RÉSEAU',
      checkSecret: 'SECRET',
      automationAria: 'Une tâche planifiée bloquée dans Discord',
      communitySurface: 'interface communauté',
      automationFired: 'La tâche planifiée daily-report vient de se déclencher',
      automationBlocked: 'bloquée',
      automationOffCopy: 'l’automatisation est désactivée par défaut — accord explicite du propriétaire requis',
      automationEnabled: 'ACTIVÉE',
      automationDisabled: 'DÉSACTIVÉE PAR DÉFAUT',
      onceEnabled: 'UNE FOIS ACTIVÉE',
      ownerEnables: 'le propriétaire active',
      policyGate: 'porte de policy',
      versionedCases: 'cas versionnés',
      memoryAria: 'Markdown fait foi et projection SQLite',
      ownerBoundary: 'frontière du propriétaire · espace local',
      longTerm: 'LONG TERME · toujours retenu',
      shortTerm: 'COURT TERME · cette session',
      promote: 'consolider',
      memoryName: 'Nom',
      memoryNameValue: 'Appelle-moi Ned',
      memoryLang: 'Langue',
      memoryLangValue: 'Répondre en chinois par défaut',
      memoryHabit: 'Habitude',
      memoryHabitValue: 'Demander avant de supprimer des fichiers',
      memoryProject: 'Projet',
      memoryProjectValue: 'Le dépôt principal est lobster0',
      memoryDoing: 'En cours',
      memoryDoingValue: 'Refonte du logo du site',
      memoryJustSaid: 'Vient de dire',
      memoryJustSaidValue: 'Les pinces partent du torse',
      memoryOnEnd: 'Fin de session',
      memoryOnEndValue: 'L’essentiel passe en long terme',
      steps: 'ÉTAPES',
      source: 'SOURCE',
      sourceValue: 'chemin d’exécution réel',
      state: 'ÉTAT',
      stateValue: 'preuve d’implémentation',
      runtime: 'RUNTIME',
      surfaces: 'INTERFACES',
      cases: 'CAS',
      shared: 'partagé',
    },
  },
} as const satisfies Record<Locale, MarketingCopy>;
