type JsonBlockProps = {
  title?: string;
  value: unknown;
  emptyMessage: string;
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }

  if (typeof value === "string") {
    return value;
  }

  return JSON.stringify(value, null, 2);
}

export function JsonBlock({ title, value, emptyMessage }: JsonBlockProps) {
  const content = formatValue(value);

  return (
    <div className="json-section">
      {title ? (
        <div className="json-section-header">
          <h3>{title}</h3>
        </div>
      ) : null}
      <pre className="json-block">{content || emptyMessage}</pre>
    </div>
  );
}
