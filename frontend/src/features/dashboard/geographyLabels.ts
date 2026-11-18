import { GeographyIndex } from "@/features/dashboard/geographyModel";
import type { GeographySite } from "@/types";

export function siteLabel(site: GeographySite) {
  return `${site.name} (${site.code})`;
}

export function placeOf(index: GeographyIndex, site: GeographySite) {
  const city = index.cityById.get(site.city_id);
  const country = city ? index.countryById.get(city.country_id) : undefined;
  return [city?.name, country?.name].filter(Boolean).join(", ");
}
