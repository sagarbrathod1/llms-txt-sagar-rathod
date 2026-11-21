import { GenerateForm } from "./generate-form";

// A server component: the text ships as plain HTML, and only the form needs JavaScript.
export default function Home() {
  return (
    <div className="space-y-8 pt-[12vh] text-center">
      <h1 className="text-2xl font-medium tracking-tight sm:text-3xl">
        Get an{" "}
        <a
          href="https://llmstxt.org/"
          className="underline decoration-border underline-offset-4 hover:decoration-foreground"
        >
          llms.txt
        </a>{" "}
        for any website, kept up to date.
      </h1>
      <GenerateForm />
    </div>
  );
}
