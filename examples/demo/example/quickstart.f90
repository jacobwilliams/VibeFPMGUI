!> An example program (fpm run --example quickstart).
program quickstart
    use demo, only: wp, linspace, mean, to_string
    implicit none  
    print '(a)', "mean of 1..10 = "//to_string(mean(linspace(1.0_wp, 10.0_wp, 10)))
end program quickstart
