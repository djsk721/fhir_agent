type JsonBlockProps = {
  title?: string;
  value: unknown;
  emptyMessage: string;
  compact?: boolean;
  headingLevel?: "h3" | "h4";
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

export function JsonBlock({
  title,
  value,
  emptyMessage,
  compact = false,
  headingLevel = "h3",
}: JsonBlockProps) {
  const content = formatValue(value);
  const HeadingTag = headingLevel;

  return (
    <div className="json-section">
      {title ? (
        <div className="json-section-header">
          <HeadingTag>{title}</HeadingTag>
        </div>
      ) : null}
      <pre className={`json-block${compact ? " json-block--compact" : ""}`}>{content || emptyMessage}</pre>
    </div>
  );
}
