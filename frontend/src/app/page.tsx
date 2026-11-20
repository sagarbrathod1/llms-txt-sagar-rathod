import { GenerateForm } from "./generate-form";

// A server component: the text ships as plain HTML, and only the form needs JavaScript.
export default function Home() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold">Generate an llms.txt for any website</h1>
        <p className="text-muted">
          Enter a site and we crawl it, build an{" "}
          <a href="https://llmstxt.org/" className="underline">
            llms.txt
          </a>{" "}
          from its pages, and keep re-checking it so the file stays current.
        </p>
      </div>
      <GenerateForm />
    </div>
  );
}
