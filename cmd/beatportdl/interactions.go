package main

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"unspok3n/beatportdl/config"
	"unspok3n/beatportdl/internal/beatport"
)

func Setup() (cfg *config.AppConfig, cachePath string, err error) {
	configFilePath, exists, err := FindConfigFile()
	if err != nil {
		return nil, "", err
	}

	if !exists {
		fmt.Println("Config file not found, creating a new one:", configFilePath)

		fmt.Print("Username: ")
		username := GetLine()
		fmt.Print("Password: ")
		password := GetLine()
		fmt.Print("Downloads directory: ")
		downloadsDir := GetLine()

		cfg := &config.AppConfig{
			Username:           username,
			Password:           password,
			DownloadsDirectory: downloadsDir,
		}

		fmt.Println("1. Lossless (44.1 khz FLAC)\n2. High (256 kbps AAC)\n3. Medium (128 kbps AAC)\n4. Medium HLS (128 kbps AAC)")
		for {
			fmt.Print("Quality: ")
			qualityNumber := GetLine()
			switch qualityNumber {
			case "1":
				cfg.Quality = "lossless"
			case "2":
				cfg.Quality = "high"
			case "3":
				cfg.Quality = "medium"
			case "4":
				cfg.Quality = "medium-hls"
			default:
				fmt.Println("Invalid quality")
				continue
			}
			break
		}

		if err := cfg.Save(configFilePath); err != nil {
			return nil, configFilePath, fmt.Errorf("save config: %w", err)
		}
	}

	parsedConfig, err := config.Parse(configFilePath)
	if err != nil {
		return nil, configFilePath, fmt.Errorf("load config: %w", err)
	}

	cacheFilePath, exists, err := FindCacheFile()
	if err != nil {
		return nil, configFilePath, fmt.Errorf("get executable path: %w", err)
	}

	return parsedConfig, cacheFilePath, nil
}

func (app *application) mainPrompt() {
	fmt.Print("\nEnter label/artist URL, search query, or label name: ")
	input := GetLine()
	input = strings.TrimSpace(input)

	if strings.HasPrefix(input, "https://www.beatport.com") || strings.HasPrefix(input, "https://www.beatsource.com") {
		// Label URLs get the interactive wizard; everything else downloads directly
		if strings.Contains(input, "/label/") || strings.Contains(input, "/artist/") {
			app.labelWizard(input)
		} else {
			app.urls = append(app.urls, input)
		}
	} else {
		app.search(input)
	}
}

// labelWizard scans a label/artist URL and walks the user through genre/subgenre/date
// selection before queuing the download.
func (app *application) labelWizard(rawURL string) {
	link, err := app.bp.ParseUrl(rawURL)
	if err != nil {
		fmt.Println("Could not parse URL:", err)
		return
	}

	var inst *beatport.Beatport
	switch link.Store {
	case beatport.StoreBeatport:
		inst = app.bp
	case beatport.StoreBeatsource:
		inst = app.bs
	default:
		fmt.Println("Unsupported store")
		return
	}

	// --- Scan ---
	var stats *scanStats
	switch link.Type {
	case beatport.LabelLink:
		label, err := inst.GetLabel(link.ID)
		if err != nil {
			fmt.Println("Could not fetch label:", err)
			return
		}
		fmt.Printf("\nScanning %s — please wait...\n", label.Name)
		stats, err = scanLabel(inst, link)
		if err != nil {
			fmt.Println("Scan error:", err)
			return
		}
	case beatport.ArtistLink:
		artist, err := inst.GetArtist(link.ID)
		if err != nil {
			fmt.Println("Could not fetch artist:", err)
			return
		}
		fmt.Printf("\nScanning %s — please wait...\n", artist.Name)
		stats, err = scanArtist(inst, link)
		if err != nil {
			fmt.Println("Scan error:", err)
			return
		}
	default:
		app.urls = append(app.urls, rawURL)
		return
	}

	fmt.Printf("\nFound %d tracks total.\n", stats.total)

	// --- Genre selection ---
	genres := rankMap(stats.genres)
	app.config.FilterGenres = selectFromList("\nGenres", genres)

	// --- Subgenre selection ---
	subgenres := rankMap(stats.subgenres)
	if len(subgenres) > 0 {
		app.config.FilterSubgenres = selectFromList("\nSubgenres", subgenres)
	}

	// --- Date range ---
	fmt.Print("\nDownload from date (e.g. 1996 or 1996-06-01, Enter for all): ")
	app.config.FilterPublishDateFrom = normaliseDate(strings.TrimSpace(GetLine()))

	fmt.Print("Download up to date   (e.g. 2024 or 2024-12-31, Enter for all): ")
	app.config.FilterPublishDateTo = normaliseDateTo(strings.TrimSpace(GetLine()))

	// --- Summary ---
	fmt.Println("\n--- Download filter summary ---")
	if len(app.config.FilterGenres) > 0 {
		fmt.Println("  Genres:    ", strings.Join(app.config.FilterGenres, ", "))
	} else {
		fmt.Println("  Genres:     all")
	}
	if len(app.config.FilterSubgenres) > 0 {
		fmt.Println("  Subgenres: ", strings.Join(app.config.FilterSubgenres, ", "))
	} else {
		fmt.Println("  Subgenres:  all")
	}
	dateRange := "all time"
	if app.config.FilterPublishDateFrom != "" && app.config.FilterPublishDateTo != "" {
		dateRange = app.config.FilterPublishDateFrom + " → " + app.config.FilterPublishDateTo
	} else if app.config.FilterPublishDateFrom != "" {
		dateRange = app.config.FilterPublishDateFrom + " → present"
	} else if app.config.FilterPublishDateTo != "" {
		dateRange = "up to " + app.config.FilterPublishDateTo
	}
	fmt.Println("  Dates:     ", dateRange)

	fmt.Print("\nStart download? (y/n): ")
	if strings.ToLower(strings.TrimSpace(GetLine())) != "y" {
		fmt.Println("Cancelled.")
		return
	}

	app.urls = append(app.urls, rawURL)
}

// selectFromList prints a numbered list and returns the names the user chose.
// Returns nil (no filter) if user presses Enter; returns all if user types *.
func selectFromList(heading string, entries []rankEntry) []string {
	if len(entries) == 0 {
		return nil
	}
	fmt.Printf("%s found:\n", heading)
	for i, e := range entries {
		fmt.Printf("  %2d. %-42s %d tracks\n", i+1, e.name, e.count)
	}
	fmt.Print("Select (e.g. 1,3  |  * for all  |  Enter to skip filter): ")
	input := strings.TrimSpace(GetLine())

	if input == "" {
		return nil
	}
	if input == "*" {
		names := make([]string, len(entries))
		for i, e := range entries {
			names[i] = e.name
		}
		return names
	}

	var selected []string
	for _, part := range strings.Split(input, ",") {
		part = strings.TrimSpace(part)
		n, err := strconv.Atoi(part)
		if err != nil || n < 1 || n > len(entries) {
			fmt.Printf("  (ignored invalid selection: %q)\n", part)
			continue
		}
		selected = append(selected, entries[n-1].name)
	}
	return selected
}

// normaliseDateFrom accepts "1996", "1996-06", or "1996-06-01" and returns "YYYY-MM-DD" (start of period).
func normaliseDate(input string) string {
	return normaliseDateBound(input, false)
}

// normaliseDateTo resolves to the end of the given year or month.
func normaliseDateTo(input string) string {
	return normaliseDateBound(input, true)
}

func normaliseDateBound(input string, endOfPeriod bool) string {
	input = strings.TrimSpace(input)
	if input == "" {
		return ""
	}
	switch len(input) {
	case 4: // "1996"
		if endOfPeriod {
			return input + "-12-31"
		}
		return input + "-01-01"
	case 7: // "1996-06"
		if endOfPeriod {
			return input + "-31" // good enough for string comparison purposes
		}
		return input + "-01"
	default:
		return input
	}
}

func (app *application) search(input string) {
	// If it looks like a label name (no spaces suggests it might be a label search)
	// try label search first, then fall back to track/release search
	var storeTag string
	storeTag, input = extractStoreTag(input)

	var inst *beatport.Beatport
	switch storeTag {
	default:
		inst = app.bp
	case "beatsource":
		inst = app.bs
	}

	// Try label search
	labelResults, err := inst.SearchLabels(input)
	if err == nil && labelResults != nil && len(labelResults.Results) > 0 {
		fmt.Println("\n[ Labels ]")
		for i, label := range labelResults.Results {
			fmt.Printf("  %2d. %s\n", i+1, label.Name)
		}

		results, _ := inst.Search(input)
		trackResultsLen := 0
		releasesResultsLen := 0
		if results != nil {
			trackResultsLen = len(results.Tracks)
			releasesResultsLen = len(results.Releases)
			labelOffset := len(labelResults.Results) + 1

			if trackResultsLen+releasesResultsLen > 0 {
				fmt.Println("\n[ Tracks ]")
				for i, track := range results.Tracks {
					fmt.Printf("  %2d. %s - %s (%s)\n", i+labelOffset,
						track.Artists.Display(app.config.ArtistsLimit, app.config.ArtistsShortForm),
						track.Name.String(), track.MixName.String())
				}
				releaseOffset := labelOffset + trackResultsLen
				fmt.Println("\n[ Releases ]")
				for i, release := range results.Releases {
					fmt.Printf("  %2d. %s - %s [%s]\n", i+releaseOffset,
						release.Artists.Display(app.config.ArtistsLimit, app.config.ArtistsShortForm),
						release.Name.String(), release.Label.Name)
				}
			}

			fmt.Print("\nEnter result number(s): ")
			selInput := GetLine()
			for _, part := range strings.Split(selInput, " ") {
				n, err := strconv.Atoi(strings.TrimSpace(part))
				if err != nil {
					continue
				}
				// Labels
				if n >= 1 && n <= len(labelResults.Results) {
					app.labelWizard(labelResults.Results[n-1].StoreUrl())
					continue
				}
				// Tracks
				if results != nil && n >= labelOffset && n < labelOffset+trackResultsLen {
					app.urls = append(app.urls, results.Tracks[n-labelOffset].URL)
					continue
				}
				// Releases
				releaseOffset := labelOffset + trackResultsLen
				if results != nil && n >= releaseOffset && n < releaseOffset+releasesResultsLen {
					app.urls = append(app.urls, results.Releases[n-releaseOffset].URL)
				}
			}
		} else {
			fmt.Print("\nEnter label number: ")
			selInput := GetLine()
			n, err := strconv.Atoi(strings.TrimSpace(selInput))
			if err == nil && n >= 1 && n <= len(labelResults.Results) {
				app.labelWizard(labelResults.Results[n-1].StoreUrl())
			}
		}
		return
	}

	// Fall back to track/release search
	results, err := inst.Search(input)
	if err != nil {
		app.FatalError("beatport", err)
	}
	trackResultsLen := len(results.Tracks)
	releasesResultsLen := len(results.Releases)

	if trackResultsLen+releasesResultsLen == 0 {
		fmt.Println("No results found")
		return
	}

	fmt.Println("Search results:")
	fmt.Println("[ Tracks ]")
	for i, track := range results.Tracks {
		fmt.Printf(
			"%2d. %s - %s (%s) [%s]\n", i+1,
			track.Artists.Display(app.config.ArtistsLimit, app.config.ArtistsShortForm),
			track.Name.String(), track.MixName.String(), track.Length,
		)
	}
	fmt.Println("\n[ Releases ]")
	indexOffset := trackResultsLen + 1
	for i, release := range results.Releases {
		fmt.Printf(
			"%2d. %s - %s [%s]\n", i+indexOffset,
			release.Artists.Display(app.config.ArtistsLimit, app.config.ArtistsShortForm),
			release.Name.String(), release.Label.Name,
		)
	}
	fmt.Print("Enter the result number(s): ")
	input = GetLine()
	requestedResults := strings.Split(input, " ")
	for _, result := range requestedResults {
		resultInt, err := strconv.Atoi(result)
		if err != nil {
			fmt.Printf("invalid result number: %s\n", result)
			continue
		}
		if resultInt > releasesResultsLen+trackResultsLen || resultInt == 0 {
			fmt.Printf("invalid result number: %d\n", resultInt)
			continue
		}
		if resultInt >= indexOffset {
			app.urls = append(app.urls, results.Releases[resultInt-indexOffset].URL)
		} else {
			app.urls = append(app.urls, results.Tracks[resultInt-1].URL)
		}
	}
}

func extractStoreTag(query string) (store, trimmedQuery string) {
	re := regexp.MustCompile(`@\w+`)
	matches := re.FindAllString(query, -1)
	if len(matches) > 0 {
		store = strings.TrimPrefix(matches[0], "@")
		trimmedQuery = re.ReplaceAllString(query, "")
		trimmedQuery = strings.TrimSpace(trimmedQuery)
	} else {
		trimmedQuery = query
	}
	return store, trimmedQuery
}

func (app *application) parseTextFile(path string) {
	file, err := os.Open(path)
	defer file.Close()
	if err != nil {
		app.FatalError("read input text file", err)
	}
	scanner := bufio.NewScanner(file)
	scanner.Split(bufio.ScanLines)

	for scanner.Scan() {
		app.urls = append(app.urls, scanner.Text())
	}
}

var (
	ErrUnsupportedLinkType  = errors.New("unsupported link type")
	ErrUnsupportedLinkStore = errors.New("unsupported link store")
)
