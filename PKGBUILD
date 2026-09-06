# Maintainer: DVR <dvrlabs@gmail.com>
pkgname=steam-yoink-git
_pkgname=steam-yoink
pkgver=r6.16c7346
pkgrel=1
pkgdesc="Lift a working non-Steam game out of Steam and run it standalone with umu"
arch=('any')
url="https://github.com/dvrlabs/steam-yoink"
license=('MIT')
depends=('python' 'pyside6' 'umu-launcher')
optdepends=('desktop-file-utils: validate and refresh installed .desktop entries'
            'xdg-utils: open game folders from within the app')
makedepends=('git')
provides=("$_pkgname")
conflicts=("$_pkgname")
source=("git+$url.git")
sha256sums=('SKIP')

pkgver() {
    cd "$srcdir/$_pkgname"
    printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}

package() {
    cd "$srcdir/$_pkgname"
    install -Dm755 steam_yoink.py \
        "$pkgdir/usr/bin/steam-yoink"
    install -Dm644 steam-yoink.desktop \
        "$pkgdir/usr/share/applications/steam-yoink.desktop"
    install -Dm644 steam-yoink.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/steam-yoink.svg"
    install -Dm644 README.md \
        "$pkgdir/usr/share/doc/$_pkgname/README.md"
    install -Dm644 LICENSE \
        "$pkgdir/usr/share/licenses/$_pkgname/LICENSE"
}
