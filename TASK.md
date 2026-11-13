# Automated llms.txt Generator

## Objective

Develop a tool that automatically generates an `llms.txt` file for a large variety of websites by analyzing its structure and content. Additionally, implement a mechanism to keep the `llms.txt` file updated as the website evolves.

## Background

The `llms.txt` file is a proposed standard designed to help Large Language Models (LLMs) better understand and interact with website content. Similar to `robots.txt`, which guides search engine crawlers, `llms.txt` provides structured information tailored for AI systems, enhancing their ability to process and utilize web data effectively.

## Assignment Tasks

Build a web application that allows a user to input a website URL and receive a generated `llms.txt` file. The design and user experience of this tool is up to you.

1. **Website Analysis and Content Extraction**
   Develop a crawler that traverses the website to identify key pages and extract relevant metadata, such as titles, descriptions, and URLs.

2. **llms.txt File Generation**
   Structure the extracted data into the `llms.txt` format. Your implementation should conform to the spec at [llmstxt.org](https://llmstxt.org/).

3. **Automated Updates**
   Implement a monitoring system that detects changes in the website's structure or content and updates the `llms.txt` file accordingly.

4. **Documentation**
   Provide clear instructions on how to set up, configure, and use the tool.

It is up to you to design how the user interacts with this tool. Build a web application the user directly visits to create the `llms.txt`.

## Evaluation Criteria

- **Functionality:** The tool should accurately generate an `llms.txt` file that reflects the website's structure and content. It should work for a large variety of websites.
- **Code Quality:** The code should be well-structured, readable, and maintainable.
- **Product Thinking & Creativity:** We encourage candidates to go beyond the baseline requirements. Show us how you think about building a great product, not just a working solution.
- **Documentation:** Clear and comprehensive documentation should accompany the tool, facilitating easy setup and usage.

## Additional Resources

- **llms.txt Specification:** <https://llmstxt.org/>
- **Examples:** <https://llmstxt.site/>
  _This is a public directory. The `llms.txt` files listed may not meet the specification. It is for reference only._
- **Getting Started with llms.txt:** <https://llmstxthub.com/guides/getting-started-llms-txt>

## Deliverables

- [ ] A live, deployed version of the application (hosting platform of your choice)
- [ ] Source code in a GitHub repo
  - Repo name should follow the pattern `llms-txt-{first_name}-{last_name}`
- [ ] A `README.md` with setup and deployment instructions
- [ ] Screenshots of the project, or alternatively a short demo video
