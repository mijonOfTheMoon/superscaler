Name:           superscaler
Version:        1.1.5
Release:        1%{?dist}
Summary:        Zero downtime supervisor worker autoscaler based on Redis queue depth
License:        MIT
Source0:        %{name}-%{version}.tar.gz
Source1:        superscaler.conf
Source2:        superscaler.service
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
Requires:       python3
Requires:       python3-pip
AutoReqProv:    no

%description
Superscaler monitors Redis queues and dynamically scales supervisor worker
process groups using a custom rpc plugin for zero downtime scaling.

%prep
%autosetup

%build
python3 -m build --wheel --no-isolation

%install
pip3 install --root=%{buildroot} --no-deps --ignore-installed dist/*.whl

mkdir -p %{buildroot}%{_sysconfdir}/superscaler
mkdir -p %{buildroot}%{_unitdir}

install -m 644 %{SOURCE1} %{buildroot}%{_sysconfdir}/superscaler/superscaler.conf
install -m 644 %{SOURCE2} %{buildroot}%{_unitdir}/superscaler.service

%files
%license LICENSE
%{python3_sitelib}/superscaler/
%{python3_sitelib}/superscaler_plugin/
%{python3_sitelib}/superscaler-%{version}*.dist-info/
%{_bindir}/superscaler
%config(noreplace) %{_sysconfdir}/superscaler/superscaler.conf
%{_unitdir}/superscaler.service

%post
pip3 install 'redis>=4.0.0' 2>/dev/null || :
%systemd_post superscaler.service

%preun
%systemd_preun superscaler.service

%postun
if [ $1 -eq 0 ]; then
    # Full uninstall, not upgrade
    pip3 uninstall -y redis 2>/dev/null || :
    rm -rf %{_sysconfdir}/superscaler 2>/dev/null || :
    rmdir %{_localstatedir}/log/superscaler 2>/dev/null || :
fi
%systemd_postun superscaler.service

%changelog
* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.5-1
- Make poll_interval parameter optional with default of 10

* Thu Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.4-1
- Rename configuration parameter group_name to program_name system-wide

* Fri Feb 27 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.3-1
- Make scaling bounds and cooldowns optional with default values
- Update target params in conf template and README

* Fri Feb 27 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.2-1
- Add version checking feature

* Thu Feb 27 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.1-1
- Fix documentation in entire codebase
- Change unix socket path to include unix:// in config file

* Wed Feb 26 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.1.0-1
- Fix config update using line parser instead of regex
- Fix monotonic clock usage in main sleep loop
- Allow scale up during pending scale down operations
- Fix confirm scale down ordering to prevent state divergence
- Add pending timeout to clear stuck pending entries
- Remove defaults section, all target params now mandatory
- Remove http xml rpc support, unix socket only
- Add unix_socket_path config option
- Add pending_timeout config parameter
- Clean up rpm packaging and uninstall residue
- Remove log directory creation, output goes to journald

* Tue Feb 25 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.0.2-1
- Add pip3 install redis in post script
- Add python3-pip as runtime dependency

* Tue Feb 25 2026 Hasbi Mizan <devopshasbi@gmail.com> - 1.0.0-1
- Initial release