#!/usr/bin/env python3

import subprocess
import tempfile
import optparse
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import requests

parser = optparse.OptionParser()
parser.add_option("--no-cache", action="store_true",
                  help="Run `docker build` with the `--no-cache` option to ignore existing images")
parser.add_option("--parallel", "-j", type="int", default=1,
                  help="Run up to this many builds in parallel")
parser.add_option("--distro", "-d", type="string", default="",
                  help="Build only this distro; should be DISTRO-CODE or DISTRO-CODE/ARCH, "
                       "e.g. debian-sid/amd64")
parser.add_option('--no-push', action="store_true",
                  help="push built images to docker repository")

(options, args) = parser.parse_args()

registry_base = 'reg.oxen.rocks:80/'
registry_insecure = True

playwright_tag = 'playwright:v1.37.0'

session_desktop_branches = ('unstable', 'clearnet', 'master')


distros = [*(('debian', x) for x in ('sid', 'stable', 'testing', 'trixie', 'bookworm', 'bullseye', 'buster')),
           *(('ubuntu', x) for x in ('rolling', 'lts',
               #'noble',
               'mantic', 'lunar', 'jammy', 'focal', 'bionic')),
           *(('session-desktop-builder', x) for x in session_desktop_branches),
           *((playwright_tag, x) for x in ('jammy', )),
           *(('appium', x) for x in ('34', )),
           ]

if options.distro:
    d = options.distro.rsplit('-', 1)
    if len(d) != 2 or d[0] not in ('debian', 'ubuntu', playwright_tag, 'session-desktop-builder', 'appium') or not d[1]:
        print(f"Bad --distro value '{options.distro}'", file=sys.stderr)
        sys.exit(1)
    distros = [(d[0], d[1].split('/')[0])]

manifests = {}  # "image:latest": ["image/amd64", "image/arm64v8", ...]
manifestlock = threading.Lock()

dep_jobs = {}  # image => [remaining_jobs, queue_dep_jobs_callbacks...]
cancel_jobs = False
jobs_lock = threading.Lock()


def arches(distro):
    if options.distro and '/' in options.distro:
        arch = options.distro.split('/')
        if arch[1] not in ('amd64', 'i386', 'arm64v8', 'arm32v7'):
            print(f"Bad --distro value '{options.distro}'", file=sys.stderr)
            sys.exit(1)
        return [arch[1]]

    if distro[0] == playwright_tag:
        return ['amd64']
    if distro[0].startswith('session-desktop-builder'):
        return ['amd64']  # FIXME: we ought to be able to remove this and use the below
    if distro[0].startswith('appium'):
        return ['amd64']  # FIXME: we ought to be able to remove this and use the below


    a = ['amd64', 'arm64v8']
    if distro[0] == 'debian' or distro == ('ubuntu', 'bionic'):
        a.append('i386')  # i386 builds don't work on later ubuntu, or on nodejs
    return a


hacks = {
    registry_base + 'ubuntu-bionic-builder': """g++-8 gpg wget \
            && mkdir -p /usr/lib/x86_64-linux-gnu/pgm-5.2/include \
    && wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /usr/share/keyrings/kitware-archive-keyring.gpg >/dev/null \
    && apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y """,
}


failure = False

lineno = 0
linelock = threading.Lock()


def print_line(myline, value):
    linelock.acquire()
    global lineno
    if sys.__stdout__.isatty():
        jump = lineno - myline
        print(f"\033[{jump}A\r\033[K{value}\033[{jump}B\r", end='')
        sys.stdout.flush()
    else:
        print(value)
    linelock.release()


def run_or_report(*args, myline, cwd=None):
    try:
        subprocess.run(
            args, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf8', cwd=cwd)
    except subprocess.CalledProcessError as e:
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as log:
            log.write(f"Error running {' '.join(args)}: {e}\n\nOutput:\n\n".encode())
            log.write(e.output.encode())
            global failure
            failure = True
            print_line(myline, f"\033[31;1mError! See {log.name} for details")
            raise e


def build_tag(tag_base, arch, contents, *, manifest_now=False):
    if failure:
        raise ChildProcessError()

    linelock.acquire()
    global lineno
    myline = lineno
    lineno += 1
    print()
    linelock.release()

    with tempfile.TemporaryDirectory(dir='.') as dockerdir:
        with open(dockerdir + '/Dockerfile', 'w') as f:
            f.write(contents)

        old_tag_base = tag_base.replace("/", "/lokinet-ci-", 1)

        tag = f'{tag_base}/{arch}'
        old_tag = f'{old_tag_base}/arch'
        print_line(myline,     f"\033[33;1mRebuilding        \033[35;1m{tag}\033[0m")
        run_or_report('docker', 'build', '--pull', '-t', tag,
                      *(('--no-cache',) if options.no_cache else ()), '.', myline=myline, cwd=dockerdir)
        if options.no_push:
            print_line(myline, f"\033[33;1mSkip Push         \033[35;1m{tag}\033[0m")
        else:
            print_line(myline, f"\033[33;1mPushing           \033[35;1m{tag}\033[0m")
            run_or_report('docker', 'push', tag, myline=myline)
            run_or_report('docker', 'tag', tag, old_tag, myline=myline)
            run_or_report('docker', 'push', old_tag, myline=myline)

        print_line(myline,     f"\033[32;1mFinished build    \033[35;1m{tag}\033[0m")

        latest = tag_base + ':latest'
        old_latest = old_tag_base + ':latest'
        global manifests
        with manifestlock:
            if latest in manifests:
                manifests[latest].append(tag)
            else:
                manifests[latest] = [tag]
            if old_latest in manifests:
                manifests[old_latest].append(old_tag)
            else:
                manifests[old_latest] = [old_tag]

        if manifest_now:
            push_manifest(tag_base)


def check_done_build(tag):
    """
    Check if we're done build all the arch builds for 'tag' and if so, push the manifest and then
    start dependent jobs (if any).
    """
    done, depjobs = False, []
    with jobs_lock:
        if tag not in dep_jobs:
            done = True
        else:
            deps = dep_jobs[tag]
            assert deps[0] > 0
            deps[0] -= 1
            if deps[0] == 0:
                done = True
                depjobs = deps[1:]

    if done:
        push_manifest(tag)

        if depjobs:
            for q in depjobs:
                q()


def distro_build_base(distro, arch, *, initial_debian=False):
    skip_build = not initial_debian and (distro, arch) in [
            (('debian', 'stable'), 'amd64'),
            (('debian', 'bookworm'), 'amd64'),
            (('debian', 'sid'), 'amd64')]

    tag = f'{registry_base}{distro[0]}-{distro[1]}-base'
    codename = 'latest' if distro == ('ubuntu', 'lts') else distro[1]
    if not skip_build:
        build_tag(tag, arch, f"""
FROM {arch}/{distro[0]}:{codename}
RUN /bin/bash -c 'echo "man-db man-db/auto-update boolean false" | debconf-set-selections'
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 -q autoremove -y
""", manifest_now=initial_debian)

    if not initial_debian:
        check_done_build(tag)


def distro_build_builder(distro, arch):
    """
    (distro)-(codename)-builder: Deb builder image used for building debs; we add the basic tools we
    use to build debs, not including things that should come from the dependencies in the
    debian/control file.
    """
    base = f'{registry_base}{distro[0]}-{distro[1]}-base'
    tag = f'{registry_base}{distro[0]}-{distro[1]}-builder'
    build_tag(tag, arch, f"""
FROM {base}/{arch}
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 --no-install-recommends -q install -y \
        ccache \
        devscripts \
        equivs \
        g++ \
        git \
        git-buildpackage \
        openssh-client \
        {hacks.get(tag, '')}
""")

    check_done_build(tag)


def distro_build(distro, arch):
    """
    (distro)-(codename): Basic image we use for most builds.  This takes the -builder and adds most
    dependencies found in our packages.
    """
    builder = f'{registry_base}{distro[0]}-{distro[1]}-builder'
    tag = f'{registry_base}{distro[0]}-{distro[1]}'
    build_tag(tag, arch, f"""
FROM {builder}/{arch}
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 --no-install-recommends -q install -y \
        automake \
        ccache \
        cmake \
        curl \
        eatmydata \
        g++ \
        gdb \
        git \
        libboost-program-options-dev \
        libboost-serialization-dev \
        libboost-thread-dev \
        libcurl4-openssl-dev \
        libevent-dev \
        libgtest-dev \
        libhidapi-dev \
        libjemalloc-dev \
        libminiupnpc-dev \
        libreadline-dev \
        libsodium-dev \
        libsqlite3-dev \
        libssl-dev \
        libsystemd-dev \
        libtool \
        libunbound-dev \
        libunwind8-dev \
        libusb-1.0.0-dev \
        libuv1-dev \
        libzmq3-dev \
        lsb-release \
        make \
        nettle-dev \
        ninja-build \
        openssh-client \
        patch \
        pkg-config \
        postgresql-client \
        pybind11-dev \
        python3-coloredlogs \
        python3-cryptography \
        python3-dev \
        python3-flask \
        python3-nacl \
        python3-openssl \
        python3-pil \
        python3-pip \
        python3-protobuf \
        python3-psycopg2 \
        python3-pybind11 \
        python3-pycryptodome \
        python3-pytest \
        python3-qrencode \
        python3-setuptools \
        python3-sqlalchemy \
        python3-sqlalchemy-utils \
        python3-tabulate \
        python3-uwsgidecorators \
        qttools5-dev \
        sqlite3 \
        {hacks.get(tag, '')}
""")

    check_done_build(tag)


def debian_clang_build():
    """For debian-sid/amd64 we also build an extra one with clang+llvm"""

    tag = f'{registry_base}debian-sid-clang'
    build_tag(tag, 'amd64', f"""
FROM {registry_base}debian-sid/amd64
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 --no-install-recommends -q install -y \
        clang clang-14 clang-15 clang-16 clang-17 clang-18 \
        lld lld-14 lld-15 lld-16 lld-17 lld-18 \
        libc++-dev \
        libc++abi-dev
""", manifest_now=True)


# Android and flutter builds on top of debian-sid-base and adds a ton of android crap; we
# schedule this job as soon as the debian-sid-base/amd64 build finishes, because they easily take
# the longest and are by far the biggest images.
def android_builds():
    build_tag(registry_base + 'android', 'amd64', f"""
FROM {registry_base}debian-sid-base
RUN /bin/bash -c 'sed -i "s/main/main non-free/g" /etc/apt/sources.list.d/debian.sources'
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 -q install --no-install-recommends -y \
        android-sdk \
        automake \
        ccache \
        cmake \
        curl \
        git \
        google-android-ndk-r26c-installer \
        libtool \
        make \
        openssh-client \
        patch \
        pkg-config \
        wget \
        xz-utils \
        zip \
    && git clone https://github.com/Shadowstyler/android-sdk-licenses.git /tmp/android-sdk-licenses \
    && cp -a /tmp/android-sdk-licenses/*-license /usr/lib/android-sdk/licenses \
    && rm -rf /tmp/android-sdk-licenses
""", manifest_now=True)

    build_tag(registry_base + 'flutter', 'amd64', f"""
FROM {registry_base}android
RUN cd /opt \
    && curl https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.7.8-stable.tar.xz \
        | tar xJv --no-same-owner \
    && ln -s /opt/flutter/bin/flutter /usr/local/bin/ \
    && flutter upgrade --force && flutter precache
""", manifest_now=True)


# lint is a tiny build (on top of debian-bookworm-base) with just formatting checking tools
def lint_build():
    build_tag(registry_base + 'lint', 'amd64', f"""
FROM {registry_base}debian-bookworm-base
RUN apt-get -o=Dpkg::Use-Pty=0 -q install --no-install-recommends -y \
    clang-format-14 \
    clang-format-15 \
    clang-format-16 \
    eatmydata \
    git \
    jsonnet
""", manifest_now=True)


def session_desktop_builder(distro, arch):
    tag = f"{registry_base}{distro[0]}-{distro[1]}"

    repo_base = f'https://raw.githubusercontent.com/oxen-io/session-desktop/{distro[1]}'
    node_v = requests.get(f'{repo_base}/.nvmrc').content.decode()

    extra_pre, cmake = '', 'cmake'
    if tuple(map(int, node_v.split('.'))) >= (18, 16):
        basedist = 'bookworm'
    else:
        basedist = 'bullseye'
        extra_pre = f"""echo "deb http://deb.debian.org/debian bullseye-backports main" >/etc/apt/sources.list.d/bullseye-backports.list &&"""
        cmake = 'cmake/bullseye-backports'

    build_tag(tag, arch, f"""
FROM {arch}/node:{node_v}-{basedist}
RUN /bin/bash -c 'echo "man-db man-db/auto-update boolean false" | debconf-set-selections'
{'RUN dpkg --add-architecture i386' if arch == 'amd64' else ''}

RUN {extra_pre} apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 -q install --no-install-recommends -y \
        ccache \
        {cmake} \
        eatmydata \
        g++ \
        gdb \
        git \
        jq \
        make \
        ninja-build \
        openssh-client \
        patch \
        pkg-config \
        rpm \
        wget \
        {'wine32 wine' if arch == 'amd64' else ''} \
    && mkdir /session-deps \
    && cd /session-deps \
    && wget {repo_base}/package.json \
    && wget {repo_base}/yarn.lock \
    && yarn install --frozen-lockfile --ignore-scripts \
    && (cd node_modules/libsession_util_nodejs && yarn install --frozen-lockfile) \
    && yarn patch-package \
    && yarn electron-builder install-app-deps \
""")
    check_done_build(tag)


def session_desktop_playwright(distro, arch):
    # Builds on the above with extra stuff needed for playwright
    tag = f"{registry_base}{distro[0].replace('builder', 'playwright')}-{distro[1]}"

    build_tag(tag, arch, f"""
FROM {registry_base}session-desktop-builder-{distro[1]}

RUN apt-get -o=Dpkg::Use-Pty=0 -q install --no-install-recommends -y \
        libasound2 \
        libgbm1 \
        libgtk-3-0 \
        libnotify4 \
        libnss3 \
        libxss1 \
        libxtst6 \
        xauth \
        xvfb
""")
    check_done_build(tag)



def playwright_build(distro, arch):

    playwright_version = f"{distro[0]}-{distro[1]}"
    # looks like the push tag forbids ":" in it, so let's remove it
    playwright_version_push = f"{playwright_version.replace(':','')}"
    tag = f"{registry_base}{playwright_version_push}"
    build_tag(tag, arch, f"""
FROM mcr.microsoft.com/{playwright_version}
RUN echo "man-db man-db/auto-update boolean false" | debconf-set-selections \
    && apt-get -o=Dpkg::Use-Pty=0 remove -y --purge nodejs \
    && apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 -q install --no-install-recommends -y \
        cmake \
        build-essential \
        time

ENV NVM_DIR /usr/local/nvm
ENV NODE_VERSION 18.15.0
ENV SESSION_DESKTOP_ROOT /root/session-desktop
ENV NODE_PATH $NVM_DIR/v$NODE_VERSION/lib/node_modules
ENV CI 1
RUN mkdir -p /usr/local/nvm \
        && curl https://raw.githubusercontent.com/creationix/nvm/v0.39.5/install.sh | bash \
        && . $NVM_DIR/nvm.sh \
        && nvm install $NODE_VERSION \
        && nvm alias default $NODE_VERSION \
        && nvm use default \
        && git config --global --add safe.directory $SESSION_DESKTOP_ROOT
""")
    check_done_build(tag)



def appium_build(distro, arch):
    tag = f"{registry_base}{distro[0]}-{distro[1]}"
    build_tag(tag, arch, f"""
FROM openjdk:18-ea-11-jdk-slim-bullseye

ENV HOME=/root \
    LANG=en_US.UTF-8 \
    DEBIAN_FRONTEN=noninteractive \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=C.UTF-8 \
    DISPLAY=:0.0 \
    DISPLAY_WIDTH=1920 \
    DISPLAY_HEIGHT=900


WORKDIR /

SHELL ["/bin/bash", "-c"]

RUN apt-get update
RUN apt install -y ca-certificates curl git cpu-checker supervisor vim bash wget unzip xvfb x11vnc fluxbox xterm novnc net-tools htop libpulse-dev libnss3 libxcursor1 libasound2 libqt5gui5 libc++-dev libxcb-cursor0 htop tree tar gzip


ARG ARCH="x86_64"
ARG TARGET="google_apis_playstore"
ARG API_LEVEL="{distro[1]}"
ARG BUILD_TOOLS="{distro[1]}.0.0"
ARG ANDROID_API_LEVEL="android-$API_LEVEL"
ARG ANDROID_APIS="$TARGET;$ARCH"
ARG EMULATOR_PACKAGE="system-images;$ANDROID_API_LEVEL;$ANDROID_APIS"
ARG PLATFORM_VERSION="platforms;$ANDROID_API_LEVEL"
ARG BUILD_TOOL="build-tools;$BUILD_TOOLS"
ARG ANDROID_CMD="commandlinetools-linux-11076708_latest.zip"
ARG ANDROID_SDK_PACKAGES="$EMULATOR_PACKAGE $PLATFORM_VERSION $BUILD_TOOL platform-tools"

ENV ANDROID_SDK_ROOT=/opt/android
ENV PATH "$PATH:$ANDROID_SDK_ROOT/cmdline-tools/tools:$ANDROID_SDK_ROOT/cmdline-tools/tools/bin:$ANDROID_SDK_ROOT/emulator:$ANDROID_SDK_ROOT/tools/bin:$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/build-tools/$BUILD_TOOLS"
ENV DOCKER="true"

#============================================
# Install required Android CMD-line tools
#============================================
RUN wget https://dl.google.com/android/repository/$ANDROID_CMD -P /tmp && \
              unzip -d $ANDROID_SDK_ROOT /tmp/$ANDROID_CMD && \
              mkdir -p $ANDROID_SDK_ROOT/cmdline-tools/tools && cd $ANDROID_SDK_ROOT/cmdline-tools &&  mv NOTICE.txt source.properties bin lib tools/  && \
              cd $ANDROID_SDK_ROOT/cmdline-tools/tools && ls

#============================================
# Install required package using SDK manager
#============================================
RUN yes Y | sdkmanager --licenses
RUN yes Y | sdkmanager --verbose --no_https $ANDROID_SDK_PACKAGES

#============================================
# Create required emulators
#============================================

ARG EMULATOR_NAME="emulator1"
ARG EMULATOR_DEVICE="pixel_6" # all emulators are created with the pixel 6 spec for now

RUN yes | sdkmanager emulator
RUN echo "no" | avdmanager --verbose create avd --force --name "$EMULATOR_NAME" --device "$EMULATOR_DEVICE" --package "$EMULATOR_PACKAGE"

#==========================
# Install node & yarn berry
#==========================

RUN curl -sL https://deb.nodesource.com/setup_18.x | bash && \
    apt-get -qqy install nodejs && npm install -g yarn && corepack enable && \
    yarn set version 4.1.1


# Install websokify and noVNC
RUN curl -O https://bootstrap.pypa.io/get-pip.py && \
    python3 get-pip.py && \
    pip3 install --no-cache-dir \
        setuptools && \
    pip3 install -U https://github.com/novnc/websockify/archive/refs/tags/v0.11.0.tar.gz

RUN wget -O x11vnc.zip https://github.com/x11vnc/noVNC/archive/refs/heads/x11vnc.zip && \
     unzip x11vnc.zip && mv noVNC-x11vnc /usr/local/noVNC/ && ls -la /usr/local/noVNC/utils && \
    (chmod a+x /usr/local/noVNC/utils/launch.sh || \
        (chmod a+x /usr/local/noVNC/utils/novnc_proxy && \
         ln -s -f /usr/local/noVNC/utils/novnc_proxy /usr/local/noVNC/utils/launch.sh)) && \
    rm -rf /tmp/* /var/tmp/*


""")
    check_done_build(tag)



def debian_win32_cross():
    build_tag(f'{registry_base}debian-win32-cross', 'amd64', f"""
FROM {registry_base}debian-stable-base/amd64
RUN apt-get -o=Dpkg::Use-Pty=0 -q install --no-install-recommends -y \
        autoconf \
        automake \
        build-essential \
        ccache \
        cmake \
        eatmydata \
        file \
        g++-mingw-w64-x86-64 \
        git \
        gperf \
        libtool \
        make \
        ninja-build \
        nsis \
        openssh-client \
        patch \
        pkg-config \
        qttools5-dev \
        wine \
        zip \
    && update-alternatives --set x86_64-w64-mingw32-gcc /usr/bin/x86_64-w64-mingw32-gcc-posix \
    && update-alternatives --set x86_64-w64-mingw32-g++ /usr/bin/x86_64-w64-mingw32-g++-posix
""", manifest_now=True)


def debian_cross_build():
    """ build debian cross compiler image """
    tag = f'{registry_base}debian-stable-cross'
    compilers = ' '.join(f'g++-{a} gcc-{a}' for a in (
        'aarch64-linux-gnu', 'arm-linux-gnueabihf', 'mips-linux-gnu', 'mips64-linux-gnuabi64',
        'mipsel-linux-gnu', 'powerpc64le-linux-gnu'))

    build_tag(tag, 'amd64', f"""
FROM {registry_base}debian-stable/amd64
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 -q install -y {compilers}
""", manifest_now=True)


def build_docs():
    """ documentation builder image """

    build_tag(f'{registry_base}docbuilder', 'amd64', f"""
FROM {registry_base}debian-stable/amd64
RUN apt-get -o=Dpkg::Use-Pty=0 -q update \
    && apt-get -o=Dpkg::Use-Pty=0 -q dist-upgrade -y \
    && apt-get -o=Dpkg::Use-Pty=0 -q install -y doxygen mkdocs curl zip unzip tar
""", manifest_now=True)


def push_manifest(image, lokinet_ci_alias=True):
    if options.no_push:
        return

    if failure:
        raise ChildProcessError()

    latest = image + ':latest'
    with manifestlock:
        tags = list(manifests[latest])
    with linelock:
        global lineno
        myline = lineno
        lineno += 1
        print()

    manifest_extra = ['--insecure'] if registry_insecure else []

    subprocess.run(['docker', 'manifest', 'rm', latest], stderr=subprocess.DEVNULL, check=False)
    print_line(myline, f"\033[33;1mCreating manifest \033[35;1m{latest}\033[0m")
    run_or_report('docker', 'manifest', 'create', *manifest_extra, latest, *tags, myline=myline)
    print_line(myline, f"\033[33;1mPushing manifest  \033[35;1m{latest}\033[0m")
    run_or_report('docker', 'manifest', 'push', *manifest_extra, latest, myline=myline)
    print_line(myline, f"\033[32;1mFinished manifest \033[35;1m{latest}\033[0m")

    if lokinet_ci_alias:
        push_manifest(image.replace("/", "/lokinet-ci-", 1), lokinet_ci_alias=False)



def finish_jobs():
    while True:
        with jobs_lock:
            if not jobs:
                break
            j = jobs.pop(0)

        try:
            j.result()
        except (ChildProcessError, subprocess.CalledProcessError):
            with jobs_lock:
                failure = True
                dep_jobs.clear()
                for k in jobs:
                    k.cancel()


executor = ThreadPoolExecutor(max_workers=max(options.parallel, 1))
jobs = []


# Start some debian base builds on their own, because other builds depend on them and we want to get
# those (especially android/flutter) fired off as soon as possible (because it's slow and huge).
with jobs_lock:
    for base in (('debian', 'stable'), ('debian', 'bookworm'), ('debian', 'sid')):
        if base in distros:
            jobs.append(executor.submit(distro_build_base, base, 'amd64', initial_debian=True))

finish_jobs()

if options.distro:
    jobs = []
else:
    jobs = [executor.submit(b) for b in (android_builds, lint_build, debian_win32_cross)]

with jobs_lock:
    # We do some basic dependency handling here: we start off all the -base images right away, then
    # schedule -builder to follow once -base is done on *all* arches (and the manifest pushed), then
    # the unsuffixed once -builder is done on all arches with manifest pushed.
    #
    # Docker sucks balls, though, so if anything goes wrong you'll just get some useless error about
    # the manifest not existing (you also sometimes just get that randomly as a failure as well,
    # because docker).

    for d in distros:
        archlist = arches(d)
        def next_wave(build_func):
            dist = d
            arches = archlist
            def f():
                with jobs_lock:
                    for a in arches:
                        jobs.append(executor.submit(build_func, dist, a))
            return f
        def next_singleton(build_func):
            def f():
                with jobs_lock:
                    jobs.append(executor.submit(build_func))
            return f

        prefix = f"{registry_base}{d[0]}-{d[1]}"
        dep_jobs[prefix] = [len(archlist)]
        if d[0] == 'session-desktop-builder':
            build_func = session_desktop_builder
            dep_jobs[prefix] = [len(archlist), next_wave(session_desktop_playwright)]

        elif d[0] == playwright_tag:
            build_func = playwright_build

        elif d[0] == 'appium':
            build_func = appium_build

        else:
            build_func = distro_build_base
            dep_jobs[prefix + "-base"] = [len(archlist), next_wave(distro_build_builder)]
            dep_jobs[prefix + "-builder"] = [len(archlist), next_wave(distro_build)]
            if d == ('debian', 'stable') and 'amd64' in archlist:
                dep_jobs[prefix].extend(next_singleton(f) for f in (debian_cross_build, build_docs))
            if d == ('debian', 'sid') and 'amd64' in archlist:
                dep_jobs[prefix].append(next_singleton(debian_clang_build))

        for a in archlist:
            jobs.append(executor.submit(build_func, d, a))

finish_jobs()

if failure:
    print("Error(s) occured, aborting!", file=sys.stderr)
    sys.exit(1)


print("\n\n\033[32;1mAll done!\n")
