import type { ReactNode } from "react";

type PanelProps = {
  title: string;
  description: string;
  kicker?: string;
  compact?: boolean;
  actions?: ReactNode;
  children: ReactNode;
};

export function Panel({
  title,
  description,
  kicker,
  compact = false,
  actions,
  children,
}: PanelProps) {
  return (
    <section className={`panel${compact ? " panel--compact" : ""}`}>
      <header className="panel-header">
        {kicker ? <p className="panel-kicker">{kicker}</p> : null}
        <div className="panel-title-row">
          <h2 className="panel-title">{title}</h2>
          {actions}
        </div>
        <p className="panel-description">{description}</p>
      </header>
      {children}
    </section>
  );
}
