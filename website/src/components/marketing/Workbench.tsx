import Image from 'next/image';

import { marketingCopy, type Locale, type WorkflowCopy } from '@/content/site';

import { HashTabs, type HashTabItem } from './HashTabs';
import { MultiChannelDiagram } from './MultiChannelDiagram';
import { QuickStartClose } from './QuickStartClose';

interface WorkbenchProps {
  locale: Locale;
  workflows: readonly WorkflowCopy[];
}

function ScreenshotWorkflow({ workflow, index }: { workflow: WorkflowCopy; index: number }) {
  if (!workflow.image) return null;

  return (
    <article className="workflow-panel workflow-panel--screenshot">
      <div className="workflow-panel__copy">
        <span>0{index + 1} / REAL TUI EVIDENCE</span>
        <h3>{workflow.title}</h3>
        <p>{workflow.summary}</p>
        <dl>
          <div><dt>SOURCE</dt><dd>repository asset</dd></div>
          <div><dt>PIXELS</dt><dd>{workflow.image.width} × {workflow.image.height}</dd></div>
          <div><dt>STATE</dt><dd>implementation evidence</dd></div>
        </dl>
      </div>
      <figure className="workflow-shot">
        <Image
          alt={workflow.image.alt}
          height={workflow.image.height}
          priority={index === 0}
          sizes="(max-width: 820px) calc(100vw - 56px), (max-width: 1240px) 58vw, 760px"
          src={workflow.image.src}
          width={workflow.image.width}
        />
        <figcaption>
          <span>OBSERVABLE</span>
          <strong>{workflow.id === 'approval' ? 'impact → exact argv → owner decision' : 'program → argv[] → result'}</strong>
        </figcaption>
      </figure>
    </article>
  );
}

export function Workbench({ locale, workflows }: WorkbenchProps) {
  const copy = marketingCopy[locale];
  const items: HashTabItem[] = workflows.map((workflow, index) => ({
    id: workflow.id,
    label: workflow.label,
    panel:
      workflow.id === 'multi-channel' ? (
        <MultiChannelDiagram workflow={workflow} />
      ) : (
        <ScreenshotWorkflow index={index} workflow={workflow} />
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
          <p className="section-lead">{copy.workbench.lead}</p>
        </div>
        <HashTabs ariaLabel={copy.nav.workbench} items={items} />
        <QuickStartClose locale={locale} />
      </div>
    </section>
  );
}
