import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const COMPONENTS: Components = {
  a: ({ href, children }) => (
    <a href={href} rel="noopener noreferrer" target="_blank">
      {children}
    </a>
  ),
  table: ({ children }) => (
    <div className="markdown-table-wrap">
      <table>{children}</table>
    </div>
  ),
};

export function Markdown({ content }: { content: string }): React.JSX.Element {
  return (
    <div className="markdown">
      <ReactMarkdown components={COMPONENTS} remarkPlugins={[remarkGfm]}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
