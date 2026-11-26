//go:build !darwin || cgo

package tray

import (
	"context"
	"os"
	"runtime"
	"sync"
	"time"

	"fyne.io/systray"

	"github.com/hhollenstain/lbw/runner/internal/app"
)

// Options are what the tray needs from the runner.
type Options struct {
	Port int
	// Key reads the dashboard key, which exists once the runner has started.
	Key func() string
	// OpenDashboard shows the dashboard. Browse opens an LBW address.
	OpenDashboard func()
	Browse        func(url string)
	// StartRunner starts the runner when it is not running.
	StartRunner func() error
	// Every is how often the tray reads the runner. Zero means 3 seconds.
	Every time.Duration
	// Program is the tray's own binary. When an update replaces it, Restart
	// starts the tray again on the new one.
	Program string
	Restart func()
}

// Supported says this build can show a tray icon.
const Supported = true

// Run shows the icon until Quit is chosen or ctx ends. It must be called on
// the main goroutine: macOS draws menus only from the main thread.
func Run(ctx context.Context, opts Options) error {
	if opts.Every == 0 {
		opts.Every = 3 * time.Second
	}
	systray.Run(func() { ready(ctx, opts) }, func() {})
	return nil
}

type menu struct {
	status      *systray.MenuItem
	waitingHead *systray.MenuItem
	waiting     [Slots]*systray.MenuItem
	runningHead *systray.MenuItem
	running     [Slots]*systray.MenuItem
	calls       *systray.MenuItem
	open        *systray.MenuItem
	pause       *systray.MenuItem
	start       *systray.MenuItem
	update      *systray.MenuItem
	quit        *systray.MenuItem
	built       time.Time

	mu        sync.Mutex
	links     map[*systray.MenuItem]string
	paused    bool
	attention int // -1 before the first draw, then 0 or 1
}

func ready(ctx context.Context, opts Options) {
	m := &menu{links: map[*systray.MenuItem]string{}, attention: -1}
	setIcon(false)
	systray.SetTooltip("LBW Runner")
	m.status = info("LBW Runner")
	systray.AddSeparator()
	m.waitingHead = info("")
	for i := range m.waiting {
		m.waiting[i] = systray.AddMenuItem("", "Open this change in LBW")
		m.waiting[i].Hide()
	}
	m.runningHead = info("")
	for i := range m.running {
		m.running[i] = systray.AddMenuItem("", "Open this change in LBW")
		m.running[i].Hide()
	}
	m.calls = info("")
	m.calls.Hide()
	systray.AddSeparator()
	m.open = systray.AddMenuItem("Open dashboard", "Show the runner's dashboard")
	m.pause = systray.AddMenuItem("Pause new work", "Running work goes on. New jobs wait in LBW.")
	m.start = systray.AddMenuItem("Start runner", "Start the runner in the background")
	m.start.Hide()
	m.update = systray.AddMenuItem("", "Download the signed release, install it, and restart")
	m.update.Hide()
	m.built = modified(opts.Program)
	systray.AddSeparator()
	m.quit = systray.AddMenuItem("Quit tray icon", "The runner keeps running. Open LBW Runner to bring the icon back.")

	for _, item := range append(append([]*systray.MenuItem{}, m.waiting[:]...), m.running[:]...) {
		go m.follow(item, opts)
	}
	go func() {
		for {
			select {
			case <-ctx.Done():
				systray.Quit()
				return
			case <-m.open.ClickedCh:
				opts.OpenDashboard()
			case <-m.pause.ClickedCh:
				m.mu.Lock()
				paused := m.paused
				m.mu.Unlock()
				_ = SetPaused(ctx, opts.Port, opts.Key(), !paused)
				m.refresh(ctx, opts)
			case <-m.start.ClickedCh:
				_ = opts.StartRunner()
			case <-m.update.ClickedCh:
				m.update.SetTitle("Installing the update…")
				m.update.Disable()
				go func() {
					_ = InstallUpdate(ctx, opts.Port, opts.Key())
					m.refresh(ctx, opts)
				}()
			case <-m.quit.ClickedCh:
				systray.Quit()
				return
			}
		}
	}()
	go func() {
		ticker := time.NewTicker(opts.Every)
		defer ticker.Stop()
		for {
			m.refresh(ctx, opts)
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
}

// follow opens whatever change the item shows when it is clicked.
func (m *menu) follow(item *systray.MenuItem, opts Options) {
	for range item.ClickedCh {
		m.mu.Lock()
		link := m.links[item]
		m.mu.Unlock()
		if link != "" {
			opts.Browse(link)
		}
	}
}

func (m *menu) refresh(ctx context.Context, opts Options) {
	if opts.Restart != nil && !m.built.IsZero() && modified(opts.Program).After(m.built) {
		// An update replaced this binary: start the tray again on the new one.
		opts.Restart()
		return
	}
	view := Build(Fetch(ctx, opts.Port, opts.Key()), time.Now())
	m.mu.Lock()
	defer m.mu.Unlock()
	m.paused = view.Paused
	m.status.SetTitle(view.Status)
	systray.SetTooltip(view.Tooltip)
	if runtime.GOOS == "darwin" {
		// macOS shows the count beside the icon.
		systray.SetTitle(view.Count)
	}
	if attention := boolInt(view.Attention); attention != m.attention {
		setIcon(view.Attention)
		m.attention = attention
	}
	header(m.waitingHead, view.WaitingHeader)
	m.fill(m.waiting[:], view.Waiting)
	header(m.runningHead, view.RunningHeader)
	m.fill(m.running[:], view.Running)
	header(m.calls, view.Calls)
	if view.Linked {
		m.pause.Show()
		if view.Paused {
			m.pause.SetTitle("Resume new work")
		} else {
			m.pause.SetTitle("Pause new work")
		}
	} else {
		m.pause.Hide()
	}
	if view.Reachable {
		m.start.Hide()
	} else {
		m.start.Show()
	}
	if view.Update == "" {
		m.update.Hide()
	} else {
		m.update.SetTitle(view.Update)
		if view.CanUpdate {
			m.update.Enable()
		} else {
			m.update.Disable()
		}
		m.update.Show()
	}
}

// modified is when the file at path last changed, or zero.
func modified(path string) time.Time {
	if path == "" {
		return time.Time{}
	}
	info, err := os.Stat(path)
	if err != nil {
		return time.Time{}
	}
	return info.ModTime()
}

func (m *menu) fill(items []*systray.MenuItem, lines []Line) {
	for i, item := range items {
		if i >= len(lines) {
			item.Hide()
			delete(m.links, item)
			continue
		}
		item.SetTitle("    " + lines[i].Text)
		m.links[item] = lines[i].Link
		if lines[i].Link == "" {
			item.Disable()
		} else {
			item.Enable()
		}
		item.Show()
	}
}

func info(title string) *systray.MenuItem {
	item := systray.AddMenuItem(title, "")
	item.Disable()
	return item
}

func header(item *systray.MenuItem, title string) {
	if title == "" {
		item.Hide()
		return
	}
	item.SetTitle(title)
	item.Show()
}

func setIcon(attention bool) {
	switch runtime.GOOS {
	case "darwin":
		// 36 pixels draws sharp at the menu bar's 18 points on a Retina screen.
		systray.SetTemplateIcon(app.TrayIcon(36, true, attention), app.TrayIcon(36, false, attention))
	case "windows":
		systray.SetIcon(app.ICO(app.TrayIcon(32, false, attention), 32))
	default:
		systray.SetIcon(app.TrayIcon(64, false, attention))
	}
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}
