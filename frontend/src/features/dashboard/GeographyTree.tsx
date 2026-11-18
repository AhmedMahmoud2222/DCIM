import { Link } from "react-router-dom";

import { placeOf, siteLabel } from "@/features/dashboard/geographyLabels";
import { GeographyIndex, hasCoordinates } from "@/features/dashboard/geographyModel";
import type { GeographySite } from "@/types";

export interface Selection {
  countryId?: string;
  cityId?: string;
  siteId?: string;
}

export interface GeographyTreeProps {
  index: GeographyIndex;
  selection: Selection;
  onCountry: (id?: string) => void;
  onCity: (id: string) => void;
  onSite: (site: GeographySite) => void;
}

const linkButton = "rounded-sm text-left text-link underline underline-offset-2 hover:text-fg-primary";

/** The location breadcrumb, the country/city/site drill-down list and the list of sites the map cannot place. */
export function GeographyTree({ index, selection, onCountry, onCity, onSite }: GeographyTreeProps) {
  const country = selection.countryId ? index.countryById.get(selection.countryId) : undefined;
  const city = selection.cityId ? index.cityById.get(selection.cityId) : undefined;
  return (
    <div className="space-y-3 text-sm">
      <nav aria-label="Location drill-down">
        <ol className="flex flex-wrap items-center gap-1 text-xs">
          <li>
            {country ? (
              <button type="button" className={linkButton} onClick={() => onCountry(undefined)}>
                All countries
              </button>
            ) : (
              <span aria-current="location" className="font-semibold text-fg-primary">All countries</span>
            )}
          </li>
          {country && (
            <li className="before:mr-1 before:text-fg-secondary before:content-['›']">
              {city ? (
                <button type="button" className={linkButton} onClick={() => onCountry(country.id)}>
                  {country.name}
                </button>
              ) : (
                <span aria-current="location" className="font-semibold text-fg-primary">{country.name}</span>
              )}
            </li>
          )}
          {city && (
            <li className="before:mr-1 before:text-fg-secondary before:content-['›']">
              <span aria-current="location" className="font-semibold text-fg-primary">{city.name}</span>
            </li>
          )}
        </ol>
      </nav>

      <div role="region" aria-label="Authorized sites by country and city" tabIndex={0} className="relative max-h-72 overflow-y-auto rounded-sm border border-line-subtle p-2">
        <ul className="space-y-1" data-testid="geography-list">
          {index.countries.map((c) => {
            const open = c.id === selection.countryId;
            const cities = index.citiesByCountry.get(c.id) ?? [];
            return (
              <li key={c.id}>
                <button type="button" aria-expanded={open} className={linkButton} onClick={() => onCountry(open && !selection.cityId ? undefined : c.id)}>
                  {c.name}
                </button>{" "}
                <span className="text-xs text-fg-secondary">
                  {c.city_count} {c.city_count === 1 ? "city" : "cities"}, {c.site_count} {c.site_count === 1 ? "site" : "sites"}
                </span>
                {open && (
                  <ul className="ml-4 mt-1 space-y-1 border-l border-line-subtle pl-2">
                    {cities.length === 0 && <li className="text-xs text-fg-secondary">No authorized city.</li>}
                    {cities.map((ci) => {
                      const cityOpen = ci.id === selection.cityId;
                      const sites = index.sitesByCity.get(ci.id) ?? [];
                      return (
                        <li key={ci.id}>
                          <button type="button" aria-expanded={cityOpen} className={linkButton} onClick={() => (cityOpen ? onCountry(c.id) : onCity(ci.id))}>
                            {ci.name}
                          </button>{" "}
                          <span className="text-xs text-fg-secondary">
                            {ci.site_count} {ci.site_count === 1 ? "site" : "sites"}
                          </span>
                          {cityOpen && (
                            <ul className="ml-4 mt-1 space-y-1 border-l border-line-subtle pl-2">
                              {sites.length === 0 && <li className="text-xs text-fg-secondary">No site in the returned list.</li>}
                              {sites.map((s) => (
                                <li key={s.id} className="flex flex-wrap items-center gap-x-2">
                                  <button type="button" className={linkButton} aria-pressed={s.id === selection.siteId} onClick={() => onSite(s)}>
                                    {siteLabel(s)}
                                  </button>
                                  {!index.withheld && !hasCoordinates(s) && <span className="text-xs text-fg-secondary">no coordinates</span>}
                                </li>
                              ))}
                            </ul>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {index.withheld ? (
        <section aria-labelledby="site-coordinates-withheld" data-testid="coordinates-withheld-list">
          <h3 id="site-coordinates-withheld" className="text-xs font-semibold uppercase tracking-wide text-fg-secondary">
            Site coordinates
          </h3>
          <p className="mt-1 text-xs text-fg-secondary">Withheld for your access level, so no site is placed on the map. The list above still shows every authorized site.</p>
        </section>
      ) : (
        <section aria-labelledby="sites-without-coordinates" data-testid="sites-without-coordinates">
          <h3 id="sites-without-coordinates" className="text-xs font-semibold uppercase tracking-wide text-fg-secondary">
            Sites without coordinates ({index.unplaced.length})
          </h3>
          {index.unplaced.length === 0 ? (
            <p className="mt-1 text-xs text-fg-secondary">Every listed site has coordinates.</p>
          ) : (
            <ul className="mt-1 space-y-1">
              {index.unplaced.map((s) => (
                <li key={s.id} className="flex flex-wrap items-center gap-x-2">
                  <Link className="text-link underline underline-offset-2" to={`/sites/${s.id}`}>
                    {siteLabel(s)}
                  </Link>
                  <span className="text-xs text-fg-secondary">{placeOf(index, s)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
