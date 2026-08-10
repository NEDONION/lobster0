import { marketingCopy, type Locale, type WorkflowCopy } from '@/content/site';

import { FlowDiagram } from './FlowDiagram';
import { HashTabs, type HashTabItem } from './HashTabs';
import { MultiChannelDiagram } from './MultiChannelDiagram';
import { QuickStartClose } from './QuickStartClose';

interface WorkbenchProps {
  locale: Locale;
  workflows: readonly WorkflowCopy[];
}

function FlowWorkflow({ locale, workflow, index }: { locale: Locale; workflow: WorkflowCopy; index: number }) {
  if (!workflow.flow) return null;
  const zh = locale === 'zh-CN';

  return (
    <article className="workflow-panel workflow-panel--flow">
      <div className="workflow-panel__copy">
        <span>0{index + 1} / {zh ? '边界时刻' : 'BOUNDARY MOMENT'}</span>
        <h3>{workflow.title}</h3>
        <p>{workflow.summary}</p>
      </div>
      <FlowDiagram
        ariaLabel={workflow.id === 'denied'
          ? (zh ? '请求 → 参数锁定 → 拒绝 → 终止' : 'request → arguments locked → denied → stopped')
          : (zh ? '程序 → 退出码 → stderr → 如实回报' : 'program → exit code → stderr → reported as-is')}
        steps={workflow.flow}
      />
    </article>
  );
}

export function Workbench({ locale, workflows }: WorkbenchProps) {
  const copy = marketingCopy[locale];
  const items: HashTabItem[] = workflows.map((workflow, index) => ({
    id: workflow.id,
    label: workflow.label,
    panel:
      workflow.id === 'isolation' ? (
        <MultiChannelDiagram locale={locale} workflow={workflow} />
      ) : (
        <FlowWorkflow index={index} locale={locale} workflow={workflow} />
      ),
  }));

  return (
    <section className="marketing-section workbench" id="workbench" aria-labelledby="workbench-title">
      <div className="site-shell section-frame">
        <div className="section-heading-grid">
          <div>
            <p className="section-kicker">{copy.workbench.eyebrow}</p>
            <h2 id="workbench-title">{copy.workbench.title}</h2>
          </div>
        </div>
        <HashTabs ariaLabel={copy.nav.workbench} items={items} />
        <QuickStartClose locale={locale} />
      </div>
    </section>
  );
}
