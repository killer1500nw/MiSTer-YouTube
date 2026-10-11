# ModelSim rejects use-before-declare in the emu top level; move two declarations up.
undef $/;
$_ = <STDIN>;
s/^assign ADC_BUS = 'Z;/wire clk_sys; wire movie_commit;\nassign ADC_BUS = 'Z;/m;
s/^wire clk_sys;\n//m;
s/^wire movie_commit=/assign movie_commit=/m;
print;
