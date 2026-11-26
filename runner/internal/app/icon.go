package app

import (
	"bytes"
	"encoding/binary"
	"image"
	"image/color"
	"image/png"
	"math"
)

// The icon is LBW's mark: a navy tile, chevrons looking both ways, and a
// green dot between them. It is drawn here so the runner needs no image files.
var (
	navy  = color.NRGBA{0x0e, 0x3a, 0x66, 0xff}
	white = color.NRGBA{0xff, 0xff, 0xff, 0xff}
	green = color.NRGBA{0x3d, 0xce, 0x7a, 0xff}
)

// IconPNG draws the icon at size by size pixels.
func IconPNG(size int) []byte {
	img := image.NewNRGBA(image.Rect(0, 0, size, size))
	s := float64(size)
	// macOS icons leave a margin around the tile.
	inset := s * 0.09
	radius := s * 0.2
	stroke := s * 0.075
	left := [][2]float64{{0.40, 0.31}, {0.22, 0.50}, {0.40, 0.69}}
	right := [][2]float64{{0.60, 0.31}, {0.78, 0.50}, {0.60, 0.69}}
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			px, py := float64(x)+0.5, float64(y)+0.5
			tile := coverage(roundedRect(px, py, inset, inset, s-inset, s-inset, radius))
			if tile == 0 {
				continue
			}
			c := blend(color.NRGBA{}, navy, tile)
			mark := math.Min(polyline(px/s, py/s, left), polyline(px/s, py/s, right))*s - stroke/2
			c = blend(c, white, coverage(mark)*tile)
			dot := math.Hypot(px-s*0.5, py-s*0.5) - s*0.07
			c = blend(c, green, coverage(dot)*tile)
			img.SetNRGBA(x, y, c)
		}
	}
	var out bytes.Buffer
	_ = png.Encode(&out, img)
	return out.Bytes()
}

// ICNS is the macOS icon file: PNGs at 512 and 1024 pixels.
func ICNS() []byte {
	var body bytes.Buffer
	for _, entry := range []struct {
		kind string
		size int
	}{{"ic09", 512}, {"ic10", 1024}} {
		data := IconPNG(entry.size)
		body.WriteString(entry.kind)
		_ = binary.Write(&body, binary.BigEndian, uint32(len(data)+8))
		body.Write(data)
	}
	var out bytes.Buffer
	out.WriteString("icns")
	_ = binary.Write(&out, binary.BigEndian, uint32(body.Len()+8))
	out.Write(body.Bytes())
	return out.Bytes()
}

// coverage turns a signed distance (negative inside) into an antialiased alpha.
func coverage(distance float64) float64 {
	return math.Max(0, math.Min(1, 0.5-distance))
}

func roundedRect(px, py, x0, y0, x1, y1, r float64) float64 {
	cx, cy := (x0+x1)/2, (y0+y1)/2
	hx, hy := (x1-x0)/2-r, (y1-y0)/2-r
	dx, dy := math.Abs(px-cx)-hx, math.Abs(py-cy)-hy
	outside := math.Hypot(math.Max(dx, 0), math.Max(dy, 0))
	return outside + math.Min(math.Max(dx, dy), 0) - r
}

// polyline is the distance from a point to a path, in the icon's unit square.
func polyline(px, py float64, points [][2]float64) float64 {
	best := math.Inf(1)
	for i := 0; i+1 < len(points); i++ {
		best = math.Min(best, segment(px, py, points[i], points[i+1]))
	}
	return best
}

func segment(px, py float64, a, b [2]float64) float64 {
	vx, vy := b[0]-a[0], b[1]-a[1]
	t := ((px-a[0])*vx + (py-a[1])*vy) / (vx*vx + vy*vy)
	t = math.Max(0, math.Min(1, t))
	return math.Hypot(px-(a[0]+t*vx), py-(a[1]+t*vy))
}

func blend(under, over color.NRGBA, alpha float64) color.NRGBA {
	if alpha <= 0 {
		return under
	}
	mix := func(a, b uint8) uint8 { return uint8(float64(a)*(1-alpha) + float64(b)*alpha + 0.5) }
	outA := alpha + float64(under.A)/255*(1-alpha)
	return color.NRGBA{mix(under.R, over.R), mix(under.G, over.G), mix(under.B, over.B), uint8(outA*255 + 0.5)}
}

// TrayIcon draws the menu bar or tray icon: the chevrons and the dot.
//
// A template icon (macOS) is black on transparent, and macOS tints it to fit
// the menu bar; there the dot is a ring, filled when something waits on the
// person. Elsewhere it is the navy tile with a green dot, amber when something waits.
func TrayIcon(size int, template, attention bool) []byte {
	img := image.NewNRGBA(image.Rect(0, 0, size, size))
	s := float64(size)
	ink := white
	if template {
		ink = color.NRGBA{0, 0, 0, 0xff}
	}
	dotColor := green
	if attention {
		dotColor = color.NRGBA{0xe6, 0xa2, 0x1b, 0xff}
	}
	stroke := s * 0.11
	left := [][2]float64{{0.38, 0.22}, {0.12, 0.50}, {0.38, 0.78}}
	right := [][2]float64{{0.62, 0.22}, {0.88, 0.50}, {0.62, 0.78}}
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			px, py := float64(x)+0.5, float64(y)+0.5
			c := color.NRGBA{}
			if !template {
				c = blend(c, navy, coverage(roundedRect(px, py, 0, 0, s, s, s*0.22)))
			}
			mark := math.Min(polyline(px/s, py/s, left), polyline(px/s, py/s, right))*s - stroke/2
			c = blend(c, ink, coverage(mark))
			centre := math.Hypot(px-s*0.5, py-s*0.5)
			switch {
			case !template:
				c = blend(c, dotColor, coverage(centre-s*0.11))
			case attention:
				c = blend(c, ink, coverage(centre-s*0.12))
			default:
				// A ring: inside the outer edge and outside the inner one.
				c = blend(c, ink, coverage(math.Abs(centre-s*0.09)-s*0.035))
			}
			img.SetNRGBA(x, y, c)
		}
	}
	var out bytes.Buffer
	_ = png.Encode(&out, img)
	return out.Bytes()
}

// ICO wraps one PNG in a Windows icon file, which Windows reads since Vista.
func ICO(pngData []byte, size int) []byte {
	var out bytes.Buffer
	_ = binary.Write(&out, binary.LittleEndian, [3]uint16{0, 1, 1})
	dim := uint8(size)
	if size >= 256 {
		dim = 0
	}
	out.Write([]byte{dim, dim, 0, 0})
	_ = binary.Write(&out, binary.LittleEndian, [2]uint16{1, 32})
	_ = binary.Write(&out, binary.LittleEndian, [2]uint32{uint32(len(pngData)), 22})
	out.Write(pngData)
	return out.Bytes()
}
