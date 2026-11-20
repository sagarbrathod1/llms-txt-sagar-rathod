import { SiteView } from "./site-view";

// A server component only to unwrap the route param; all data loading happens in SiteView on the
// client, because the data is per-request and polled.
export default async function SitePage({ params }: PageProps<"/sites/[siteId]">) {
  const { siteId } = await params;
  return <SiteView siteId={siteId} />;
}
