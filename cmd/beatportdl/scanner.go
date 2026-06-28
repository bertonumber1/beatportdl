package main

import (
	"fmt"
	"sort"
	"sync"
	"unspok3n/beatportdl/internal/beatport"
)

type scanStats struct {
	mu        sync.Mutex
	genres    map[string]int
	subgenres map[string]int
	artists   map[string]int
	bpmMin    int
	bpmMax    int
	total     int
}

func newScanStats() *scanStats {
	return &scanStats{
		genres:    make(map[string]int),
		subgenres: make(map[string]int),
		artists:   make(map[string]int),
		bpmMin:    9999,
		bpmMax:    0,
	}
}

func (s *scanStats) add(track *beatport.Track) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.total++
	s.genres[track.Genre.Name]++
	if track.Subgenre != nil && track.Subgenre.Name != "" {
		s.subgenres[track.Subgenre.Name]++
	}
	for _, a := range track.Artists {
		s.artists[a.Name]++
	}
	if track.BPM > 0 {
		if track.BPM < s.bpmMin {
			s.bpmMin = track.BPM
		}
		if track.BPM > s.bpmMax {
			s.bpmMax = track.BPM
		}
	}
}

type rankEntry struct {
	name  string
	count int
}

func rankMap(m map[string]int) []rankEntry {
	entries := make([]rankEntry, 0, len(m))
	for k, v := range m {
		entries = append(entries, rankEntry{k, v})
	}
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].count != entries[j].count {
			return entries[i].count > entries[j].count
		}
		return entries[i].name < entries[j].name
	})
	return entries
}

func (s *scanStats) printFull() {
	fmt.Printf("\n========== Scan Results — %d tracks ==========\n", s.total)

	fmt.Println("\n[ Genres ]  (use in filter_genres:)")
	for _, e := range rankMap(s.genres) {
		fmt.Printf("  %-42s %4d tracks\n", e.name, e.count)
	}

	if len(s.subgenres) > 0 {
		fmt.Println("\n[ Subgenres ]  (use in filter_subgenres:)")
		for _, e := range rankMap(s.subgenres) {
			fmt.Printf("  %-42s %4d tracks\n", e.name, e.count)
		}
	}

	if s.bpmMax > 0 && s.bpmMin < 9999 {
		fmt.Printf("\n[ BPM Range ]  %d – %d\n", s.bpmMin, s.bpmMax)
	}

	fmt.Println("\n[ Top Artists ]  (use in filter_artists:)")
	for i, e := range rankMap(s.artists) {
		if i >= 30 {
			break
		}
		fmt.Printf("  %-42s %4d tracks\n", e.name, e.count)
	}

	fmt.Println("\n================================================")
}

// scanLabel scans all releases for a label and returns stats with live progress output.
func scanLabel(inst *beatport.Beatport, link *beatport.Link) (*scanStats, error) {
	stats := newScanStats()
	releaseCount := 0

	err := ForPaginated[beatport.Release](link.ID, link.Params, inst.GetLabelReleases, func(release beatport.Release, _ int) error {
		releaseCount++
		fmt.Printf("\r  Scanning release %d — %d tracks found so far...", releaseCount, stats.total)
		return ForPaginated[beatport.Track](release.ID, "", inst.GetReleaseTracks, func(track beatport.Track, _ int) error {
			stats.add(&track)
			return nil
		})
	})
	fmt.Println()
	return stats, err
}

// scanArtist scans all tracks for an artist and returns stats.
func scanArtist(inst *beatport.Beatport, link *beatport.Link) (*scanStats, error) {
	stats := newScanStats()
	err := ForPaginated[beatport.Track](link.ID, link.Params, inst.GetArtistTracks, func(track beatport.Track, _ int) error {
		stats.add(&track)
		fmt.Printf("\r  %d tracks scanned...", stats.total)
		return nil
	})
	fmt.Println()
	return stats, err
}

func (app *application) handleScanUrl(url string) {
	link, err := app.bp.ParseUrl(url)
	if err != nil {
		app.errorLogWrapper(url, "parse url", err)
		return
	}

	var inst *beatport.Beatport
	switch link.Store {
	case beatport.StoreBeatport:
		inst = app.bp
	case beatport.StoreBeatsource:
		inst = app.bs
	default:
		app.LogError("scan URL", ErrUnsupportedLinkStore)
		return
	}

	var stats *scanStats

	switch link.Type {
	case beatport.LabelLink:
		label, err := inst.GetLabel(link.ID)
		if err != nil {
			app.errorLogWrapper(url, "fetch label", err)
			return
		}
		fmt.Printf("Scanning label: %s (this may take a while for large catalogues)\n", label.Name)
		stats, err = scanLabel(inst, link)
		if err != nil {
			app.errorLogWrapper(url, "scan label", err)
			return
		}

	case beatport.ArtistLink:
		artist, err := inst.GetArtist(link.ID)
		if err != nil {
			app.errorLogWrapper(url, "fetch artist", err)
			return
		}
		fmt.Printf("Scanning artist: %s\n", artist.Name)
		stats, err = scanArtist(inst, link)
		if err != nil {
			app.errorLogWrapper(url, "scan artist", err)
			return
		}

	default:
		fmt.Println("--scan only works with label or artist URLs")
		return
	}

	stats.printFull()
}
